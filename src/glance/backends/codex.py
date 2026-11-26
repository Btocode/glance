"""codex CLI backend: `codex exec --json` JSONL, one agent_message per turn (no deltas)."""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator

from glance.backends.base import (
    BackendError,
    BaseBackend,
    Health,
    error_detail,
    extract_json_object,
)
from glance.backends.proc import ProcRunner
from glance.backends.usage import Usage, make_usage
from glance.backends.prompts import ANALYZE_SCHEMA
from glance.paths import runtime_dir

_AUTH = re.compile(
    r"not logged in|log ?in|unauthori[sz]ed|authenticat|\b401\b|\b403\b|api key", re.IGNORECASE
)
_RATE = re.compile(r"rate.?limit|usage limit|too many requests|\b429\b|quota", re.IGNORECASE)


def classify_error(text: str) -> str:
    """Map a codex error message (often a JSON string) to a BackendError kind."""
    if _AUTH.search(text):
        return "auth"
    if _RATE.search(text):
        return "rate_limit"
    return "unavailable"  # includes model_not_found (recorded shape)


def _events(lines: Iterator[str]) -> Iterator[dict]:
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


def _final_message(lines: Iterator[str],
                   on_usage: Callable[[Usage | None], None] | None = None) -> str:
    """The last agent_message text; raises on error / turn.failed events. `on_usage` gets the
    token counts of a `turn.completed` event."""
    text: str | None = None
    err: str | None = None
    for ev in _events(lines):
        t = ev.get("type")
        if t == "item.completed":
            item = ev.get("item") or {}
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                text = item["text"]
        elif t == "turn.completed" and on_usage is not None:
            u = ev.get("usage")
            if isinstance(u, dict):  # codex input_tokens already includes the cached ones
                on_usage(make_usage(u.get("input_tokens"), u.get("output_tokens")))
        elif t == "turn.failed":
            err = str((ev.get("error") or {}).get("message") or "turn failed")
        elif t == "error" and err is None:
            err = str(ev.get("message") or "error")
    if err is not None:
        raise BackendError(classify_error(err), error_detail(err, "codex_error"))
    if text is None:
        raise BackendError("bad_output", "no agent_message")
    return text


# Features switched off so untrusted text cannot steer the model into reading local files.
# Verified offline with `codex features list` that --disable takes effect; whether the model then
# has no tool at all could not be checked without a model call, hence "experimental".
LOCKED_FEATURES = ("shell_tool", "unified_exec", "view_image", "apps", "plugins", "multi_agent",
                   "browser_use", "computer_use")
EXPERIMENTAL = "experimental: tool lock-down not verified"


class CodexBackend(BaseBackend):
    name = "codex"
    # Findings S6.3: structured analyze took 8.6-13.6 s, over the 8 s budget. The supervisor
    # routes analyze here only when the user's config overrides this default.
    analyze_default = False

    def __init__(self, runner: ProcRunner, model: str = "", binary: str = "codex") -> None:
        self.runner, self.model, self.binary = runner, model, binary
        self._schema_path = None

    def _schema_file(self) -> str:
        if self._schema_path is None:
            path = runtime_dir() / "analyze.schema.json"
            path.write_text(json.dumps(ANALYZE_SCHEMA))  # schema only, no user text
            path.chmod(0o600)
            self._schema_path = path
        return str(self._schema_path)

    def _argv(self, schema: bool) -> list[str]:
        argv = [
            self.binary, "exec", "--json", "--ephemeral", "--skip-git-repo-check",
            "-s", "read-only", "--ignore-rules", "--ignore-user-config",  # S6.3: auth survives
        ]
        for feature in LOCKED_FEATURES:  # no shell, no file viewer, no plugins, no sub-agents
            argv += ["--disable", feature]
        argv += ["-c", 'web_search="disabled"']
        if schema:
            argv += ["--output-schema", self._schema_file()]
        if self.model:
            argv += ["-m", self.model]
        return argv + ["-"]

    @staticmethod
    def _stdin(system: str, user: str) -> str:
        return f"<system>\n{system}\n</system>\n\n{user}"

    def _run_json(self, system: str, user: str, schema: dict, timeout_s: float) -> dict:
        res = self.runner.run(self._argv(True), self._stdin(system, user), timeout_s)
        try:
            text = _final_message(iter(res.stdout.splitlines()), self._add_usage)
        except BackendError as e:
            if e.kind == "bad_output" and res.returncode:
                raise BackendError(classify_error(res.stderr), error_detail(res.stderr, "codex_exit")) from e
            raise
        try:
            obj = json.loads(text)
        except ValueError:
            return extract_json_object(text)
        if not isinstance(obj, dict):
            raise BackendError("bad_output", "not an object")
        return obj

    def _run_stream(self, system: str, user: str, timeout_s: float) -> Iterator[str]:
        lines = self.runner.stream(self._argv(False), self._stdin(system, user), timeout_s)
        yield _final_message(lines, self._add_usage)  # no deltas in the recorded shape: one chunk

    def cancel(self) -> None:
        self.runner.cancel()

    def health(self) -> Health:
        try:
            res = self.runner.run([self.binary, "login", "status"], "", 3.0)
        except BackendError as e:
            if e.kind == "unavailable":
                return Health(False, None, "codex CLI not installed")
            return Health(True, None, e.kind)
        if res.returncode == 0 and re.search(r"logged in", res.stdout, re.IGNORECASE):
            return Health(True, True, f"{res.stdout.strip() or 'signed in'} ({EXPERIMENTAL})")
        if res.returncode != 0:
            return Health(True, False, "not signed in: run `codex login`")
        return Health(True, None, "unrecognised login status")
