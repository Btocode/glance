"""gemini CLI backend.

UNVERIFIED success path: every gemini call failed auth on the spike machine (findings S6.4,
IneligibleTierError UNSUPPORTED_CLIENT), so the `-o json` envelope (`{"response": ...}`) and the
`-o stream-json` events (`{"type":"message","role":"assistant","content":...}`) below come from
gemini-cli's documentation, not recordings. Parsing is defensive. Only the failure text is recorded.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from pathlib import Path

from glance.backends import cli_path
from glance.backends.base import (
    BackendError,
    BaseBackend,
    Health,
    error_detail,
    extract_json_object,
)
from glance.backends.proc import ProcRunner

_AUTH = re.compile(
    r"IneligibleTierError|UNSUPPORTED_CLIENT|Error authenticating|not logged in|"
    r"unauthori[sz]ed|\b401\b|API key not valid|please (?:log ?in|sign in)", re.IGNORECASE)
_RATE = re.compile(r"Resource has been exhausted|quota|rate.?limit|\b429\b", re.IGNORECASE)
FIX_HINT = "gemini sign-in failed: re-authenticate by running `gemini`, or use an API key (GEMINI_API_KEY)"


def classify_error(text: str) -> str:
    # The recorded failure contains both texts; the sign-in rejection is the cause.
    if _AUTH.search(text):
        return "auth"
    if _RATE.search(text):
        return "rate_limit"
    return "unavailable"


def resolve_binary(binary: str) -> str | None:
    """Absolute path of the CLI (gemini usually lives under nvm): see `cli_path`."""
    return cli_path.find(binary)


class GeminiBackend(BaseBackend):
    name = "gemini"

    def __init__(self, runner: ProcRunner, model: str = "", binary: str = "gemini") -> None:
        self.runner, self.model, self.binary = runner, model, binary
        self._failure: str | None = None  # last classified failure kind, cleared on success

    def _argv(self, fmt: str) -> list[str]:
        argv = [
            resolve_binary(self.binary) or self.binary, "-p", "", "-o", fmt,
            "--approval-mode", "plan", "--skip-trust",
            # extensions / MCP off (UNVERIFIED flag forms, from --help only)
            "-e", "none", "--allowed-mcp-server-names", "none",
        ]
        if self.model:
            argv += ["-m", self.model]
        return argv

    @staticmethod
    def _stdin(system: str, user: str) -> str:
        return f"<system>\n{system}\n</system>\n\n{user}"

    def _fail(self, text: str, detail: str) -> BackendError:
        kind = classify_error(text)
        self._failure = kind
        return BackendError(kind, error_detail(text, detail))

    def _response_text(self, stdout: str) -> str:
        try:
            env = json.loads(stdout)
        except ValueError:
            env = None
        if isinstance(env, dict):
            if env.get("error"):
                raise self._fail(json.dumps(env["error"]), "gemini_error")
            if isinstance(env.get("response"), str):
                return env["response"]
        return stdout  # envelope shape unverified: fall back to the raw text

    def _run_json(self, system: str, user: str, schema: dict, timeout_s: float) -> dict:
        res = self.runner.run(self._argv("json"), self._stdin(system, user), timeout_s)
        if res.returncode:
            raise self._fail(res.stdout + res.stderr, "gemini_exit")
        obj = extract_json_object(self._response_text(res.stdout))
        self._failure = None
        return obj

    def _run_stream(self, system: str, user: str, timeout_s: float) -> Iterator[str]:
        err: list[str] = []
        lines = self.runner.stream(
            self._argv("stream-json"), self._stdin(system, user), timeout_s, err_sink=err
        )
        got = False
        try:
            for line in lines:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                if ev.get("type") == "message" and ev.get("role") == "assistant":
                    text = ev.get("content")
                    if isinstance(text, str) and text:
                        got = True
                        yield text
                elif ev.get("type") == "error":
                    raise self._fail(json.dumps(ev), "gemini_error")
        except BackendError as e:
            if e.kind == "unavailable" and not got:
                raise self._fail(str(e), "gemini_exit") from e
            raise
        if not got:
            if err and classify_error("".join(err)) != "unavailable":
                raise self._fail("".join(err), "gemini_exit")
            raise BackendError("bad_output", "no assistant message")
        self._failure = None

    def cancel(self) -> None:
        self.runner.cancel()

    def health(self) -> Health:
        if resolve_binary(self.binary) is None:
            return Health(False, None, "gemini CLI not installed")
        if self._failure == "auth":
            return Health(True, False, FIX_HINT)
        creds = (Path.home() / ".gemini" / "oauth_creds.json").exists()
        key = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
        # S6.4: a credentials file does not prove the account is accepted, so never True here.
        if creds or key:
            return Health(True, None, "unknown: press Test")
        return Health(True, None, "unknown: no credentials found, run `gemini` to sign in")
