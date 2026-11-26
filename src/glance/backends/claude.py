"""claude CLI backend: structured analyze via --json-schema, streaming rewrite via stream-json."""
from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor

from glance.backends.base import BackendError, BaseBackend, Health, error_detail
from glance.backends.proc import IdleProc, ProcRunner
from glance.backends.usage import Usage, make_usage

_AUTH = re.compile(
    r"log ?in|logged in|authenticat|unauthori[sz]ed|api key|oauth|credential|sign in", re.IGNORECASE
)
_RATE = re.compile(
    r"rate.?limit|usage limit|limit reached|too many requests|quota|overloaded_error", re.IGNORECASE
)


def classify_error(status: int | None, text: str) -> str:
    """Map a claude failure (HTTP-ish status and message text) to a BackendError kind."""
    if status in (401, 403) or _AUTH.search(text):
        return "auth"
    if status == 429 or _RATE.search(text):
        return "rate_limit"
    return "unavailable"


def _json_lines(lines: Iterator[str]) -> Iterator[dict]:
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


def claude_usage(obj: dict) -> Usage | None:
    """Tokens and cost from a claude `result` object (cache reads and writes count as input)."""
    u = obj.get("usage")
    u = u if isinstance(u, dict) else {}
    parts = [u.get(k) for k in ("input_tokens", "cache_creation_input_tokens",
                                "cache_read_input_tokens")]
    ints = [p for p in parts if type(p) is int and p >= 0]
    return make_usage(sum(ints) if ints else None, u.get("output_tokens"),
                      obj.get("total_cost_usd"))


def _raise_if_error(obj: dict) -> None:
    if obj.get("is_error"):
        text = str(obj.get("result") or "")
        raise BackendError(classify_error(obj.get("api_error_status"), text),
                           error_detail(text, "claude_error"))


WARM_SYSTEM = ("Follow the instructions in the <system> block of each user message "
               "exactly.")


class ClaudeBackend(BaseBackend):
    name = "claude"
    # Findings S6.2: a warm (pre-spawned, idle) process saves ~0.86 s a call for ~195 MB idle
    # RSS. The user prioritises speed, so it is on for the chosen backend when that is claude
    # (`backend.warm`, see BackendCfg). The supervisor turns it on with `set_warm`; a backend
    # that is not in use never has one. Up to two idle processes (analyze and rewrite).
    WARM_ENABLED = True

    def __init__(self, runner: ProcRunner, model: str = "haiku", binary: str = "claude",
                 warm: bool = False) -> None:
        self.runner, self.model, self.binary, self.warm = runner, model, binary, warm
        self._idle: dict[str, IdleProc] = {}
        self._idle_lock = threading.Lock()
        self._spawn_lock = threading.Lock()
        self._spawner: ThreadPoolExecutor | None = None

    # -- argv ----------------------------------------------------------------
    def _base(self, system: str) -> list[str]:
        model = ["--model", self.model] if self.model else []  # "": the CLI's own default
        return [
            self.binary, "-p", *model, "--tools", "",
            "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
            "--system-prompt", system,
        ]

    def argv_json(self, system: str, schema: dict) -> list[str]:
        return self._base(system) + [
            "--json-schema", json.dumps(schema), "--output-format", "json",
        ]

    def argv_stream(self, system: str) -> list[str]:
        return self._base(system) + [
            "--output-format", "stream-json", "--include-partial-messages", "--verbose",
        ]

    def _argv_warm(self, kind: str) -> list[str]:
        """A warm process cannot know the call's system prompt (it depends on the profile,
        tone and app), so it starts with a fixed one and the call's goes in the message."""
        from glance.backends.prompts import ANALYZE_SCHEMA

        argv = self._base(WARM_SYSTEM) + ["--input-format", "stream-json", "--output-format",
                                          "stream-json", "--verbose"]
        if kind == "analyze":
            argv += ["--json-schema", json.dumps(ANALYZE_SCHEMA)]
        else:
            argv += ["--include-partial-messages"]
        return argv

    # -- warm spawn ------------------------------------------------------------
    @property
    def _warm_on(self) -> bool:
        return self.warm and self.WARM_ENABLED

    def set_warm(self, on: bool) -> None:
        """On or off; turning it off kills the idle processes (memory back at once)."""
        was, self.warm = self.warm, on
        if not on:
            self.close()
        elif not was:
            self.prewarm("analyze")
            self.prewarm("rewrite")

    def set_model(self, model: str) -> None:
        before = self.model
        super().set_model(model)
        if self.model != before:
            self._restart_warm()  # idle processes were started with the old model

    def _drop_model(self, e: BackendError) -> bool:
        dropped = super()._drop_model(e)
        if dropped:
            self._restart_warm()
        return dropped

    def _restart_warm(self) -> None:
        self.close()
        if self._warm_on:
            self.prewarm("analyze")
            self.prewarm("rewrite")

    def prewarm(self, kind: str, system: str | None = None) -> None:
        """Start an idle process for `kind` in the background (`system` is accepted for
        callers that have one; the warm path does not need it). One per kind."""
        if not self._warm_on:
            return
        if kind not in ("analyze", "rewrite"):
            raise ValueError(f"unknown prewarm kind: {kind}")
        with self._spawn_lock:
            if self._spawner is None:  # ONE long-lived thread does every spawn: a process
                # started with PR_SET_PDEATHSIG dies when the thread that started it ends
                self._spawner = ThreadPoolExecutor(1, thread_name_prefix="claude-warm")
            self._spawner.submit(self._spawn_idle, kind)

    def wait_warm(self) -> None:
        """Block until the spawns asked for so far are done (tests)."""
        with self._spawn_lock:
            spawner = self._spawner
        if spawner is not None:
            spawner.submit(lambda: None).result()

    def _spawn_idle(self, kind: str) -> None:
        with self._idle_lock:
            if not self._warm_on:
                return
            cur = self._idle.get(kind)
            if cur and cur.alive:
                return
            try:
                self._idle[kind] = self.runner.spawn_idle(self._argv_warm(kind))
            except BackendError:
                self._idle.pop(kind, None)

    def _take_idle(self, kind: str) -> IdleProc | None:
        with self._idle_lock:
            cur = self._idle.pop(kind, None)
        return cur if cur is not None and cur.alive else None

    @staticmethod
    def _warm_message(system: str, user: str) -> str:
        content = f"<system>\n{system}\n</system>\n\n{user}"
        return json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"

    def close(self) -> None:
        with self._idle_lock:
            idle, self._idle = list(self._idle.values()), {}
        for p in idle:
            p.kill()
        with self._spawn_lock:
            spawner, self._spawner = self._spawner, None
        if spawner is not None:  # its idle thread would otherwise hold glanced's exit
            spawner.shutdown(wait=False, cancel_futures=True)

    # -- primitives --------------------------------------------------------------
    def _run_json(self, system: str, user: str, schema: dict, timeout_s: float) -> dict:
        idle = self._take_idle("analyze") if self._warm_on else None
        if idle is not None:
            try:
                result = None
                for obj in _json_lines(idle.feed(self._warm_message(system, user), timeout_s)):
                    if obj.get("type") == "result":
                        result = obj
            finally:
                idle.kill()
                self.prewarm("analyze")
            if result is None:
                raise BackendError("unavailable", "no result")
            self._add_usage(claude_usage(result))
            return self._structured(result)
        res = self.runner.run(self.argv_json(system, schema), user, timeout_s)
        try:  # the CLI prints one indented JSON object over many lines
            obj = json.loads(res.stdout)
        except ValueError:
            obj = None
        if not isinstance(obj, dict):
            obj = next(iter(reversed(list(_json_lines(iter(res.stdout.splitlines()))))), None)
        if obj is None:
            raise BackendError(
                classify_error(None, res.stdout + res.stderr) if res.returncode else "bad_output",
                error_detail(res.stdout + res.stderr, "no json") if res.returncode else "no json",
            )
        self._add_usage(claude_usage(obj))
        return self._structured(obj)

    @staticmethod
    def _structured(obj: dict) -> dict:
        _raise_if_error(obj)
        so = obj.get("structured_output")
        if isinstance(so, dict):
            return so
        try:
            parsed = json.loads(obj.get("result") or "")
        except ValueError:
            parsed = None
        if not isinstance(parsed, dict):
            raise BackendError("bad_output", "no structured_output")
        return parsed

    def _run_stream(self, system: str, user: str, timeout_s: float) -> Iterator[str]:
        idle = self._take_idle("rewrite") if self._warm_on else None
        try:
            if idle is not None:
                lines = idle.feed(self._warm_message(system, user), timeout_s)
            else:
                lines = self.runner.stream(self.argv_stream(system), user, timeout_s)
            yield from self._parse_stream(lines, self._add_usage)
        finally:
            if idle is not None:
                idle.kill()
                self.prewarm("rewrite")

    @staticmethod
    def _parse_stream(lines: Iterator[str],
                      on_usage: Callable[[Usage | None], None] | None = None) -> Iterator[str]:
        sent = ""
        final: str | None = None
        for obj in _json_lines(lines):
            t = obj.get("type")
            if t == "stream_event":
                ev = obj.get("event") or {}
                d = ev.get("delta") or {}
                if ev.get("type") == "content_block_delta" and d.get("type") == "text_delta":
                    text = d.get("text") or ""
                    if text:
                        sent += text
                        yield text
            elif t == "result":
                if on_usage is not None:
                    on_usage(claude_usage(obj))
                _raise_if_error(obj)
                final = obj.get("result") if isinstance(obj.get("result"), str) else None
        if final is None:
            if not sent:
                raise BackendError("bad_output", "no result")
            return
        if final.startswith(sent):
            if len(final) > len(sent):
                yield final[len(sent):]
        elif not sent:
            yield final

    def cancel(self) -> None:
        self.runner.cancel()

    def health(self) -> Health:
        try:
            res = self.runner.run([self.binary, "auth", "status", "--json"], "", 3.0)
        except BackendError as e:
            if e.kind == "unavailable":
                return Health(False, None, "claude CLI not installed")
            return Health(True, None, e.kind)
        try:  # the CLI prints one indented JSON object over many lines
            obj = json.loads(res.stdout)
        except ValueError:
            obj = None
        if not isinstance(obj, dict):
            obj = next(iter(reversed(list(_json_lines(iter(res.stdout.splitlines()))))), None)
        if obj is None or not isinstance(obj.get("loggedIn"), bool):
            return Health(True, None, "unrecognised auth status")
        return Health(True, obj["loggedIn"], "signed in" if obj["loggedIn"] else "signed out")
