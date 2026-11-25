"""opencode backend: a private loopback `opencode serve`, calls via `opencode run --attach`.

UNVERIFIED success path: on the spike machine the server failed every prompt with a SQLite FOREIGN KEY
error (findings S6.5), so no model reply was ever recorded. The `text` events parsed below
(`{"type":"text","part":{"text":...}}`) come from opencode's documented `--format json` output;
parsing is defensive. Recorded and handled: the server URL line, `type:"error"` events (-> unavailable,
`sessionID` key), `session delete`, and `OPENCODE_SERVER_PASSWORD`. The message goes on stdin
(S6.5 saw stdin accepted but never answered); if a working opencode turns out to ignore stdin, switch
to `POST /session/{id}/message` via httpx rather than argv.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import signal
import subprocess
import threading
from collections.abc import Iterator

from glance.backends.base import (
    BackendError,
    BaseBackend,
    Health,
    error_detail,
    extract_json_object,
)
from glance.backends.proc import ProcRunner, _terminate_group, empty_dir

_URL = re.compile(r"listening on (http://127\.0\.0\.1:\d+)")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CREDS = re.compile(r"(\d+)\s+credentials?", re.IGNORECASE)
_AUTH = re.compile(r"unauthori[sz]ed|not logged in|authenticat|api key|\b401\b|credential", re.IGNORECASE)
_RATE = re.compile(r"rate.?limit|usage limit|too many requests|\b429\b|quota", re.IGNORECASE)
_SERVER_BUG = re.compile(r"Unexpected server error|FOREIGN KEY|SQLite", re.IGNORECASE)
SERVER_START_TIMEOUT_S = 10.0
EXPERIMENTAL = "experimental: no verified reply"
# Lock-down config (OPENCODE_CONFIG_CONTENT, highest-precedence config source): every tool that
# could read a file, run a command or fetch a URL on behalf of untrusted text is denied.
LOCKDOWN_CONFIG = json.dumps({
    "permission": {
        "*": "deny", "bash": "deny", "read": "deny", "edit": "deny", "webfetch": "deny",
        "websearch": "deny", "external_directory": "deny", "task": "deny",
    },
    "mcp": {}, "lsp": False, "share": "disabled", "autoupdate": False,
}, sort_keys=True)
UPDATE_HINT = (
    "opencode server error (recorded: SQLite FOREIGN KEY failure): update or reinstall opencode"
)


def classify_error(text: str) -> str:
    if _AUTH.search(text):
        return "auth"
    if _RATE.search(text):
        return "rate_limit"
    return "unavailable"  # includes the recorded UnknownError / "Unexpected server error"


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


class OpencodeBackend(BaseBackend):
    name = "opencode"

    def __init__(self, runner: ProcRunner, model: str = "", binary: str = "opencode") -> None:
        self.runner, self.model, self.binary = runner, model, binary
        self._password = secrets.token_hex(16)  # 32 hex chars; env only, never argv
        self._server: subprocess.Popen | None = None
        self._url: str | None = None
        self._lock = threading.Lock()
        self._failure: str | None = None  # "server" | "auth" | ... of the last failed call

    @property
    def _env(self) -> dict:
        return {"OPENCODE_SERVER_PASSWORD": self._password, "OPENCODE_CONFIG_CONTENT": LOCKDOWN_CONFIG}

    # -- server --------------------------------------------------------------
    def ensure_server(self) -> str:
        with self._lock:
            if self._server is not None and self._server.poll() is None and self._url:
                return self._url
            self._reap()
            argv = [self.binary, "serve", "--port", "0", "--hostname", "127.0.0.1", "--pure"]
            try:
                proc = subprocess.Popen(
                    argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, cwd=empty_dir(),
                    env={**os.environ, **self.runner.env, **self._env}, start_new_session=True,
                )
            except (FileNotFoundError, PermissionError) as e:
                raise BackendError("unavailable", type(e).__name__) from e
            found: list[str] = []
            ready = threading.Event()

            def pump() -> None:
                try:
                    for line in proc.stdout:
                        m = _URL.search(line)
                        if m and not found:
                            found.append(m.group(1))
                            ready.set()
                except (OSError, ValueError):
                    pass
                ready.set()  # EOF: server exited

            threading.Thread(target=pump, daemon=True).start()
            if not ready.wait(SERVER_START_TIMEOUT_S) or not found:
                _terminate_group(proc.pid)
                raise BackendError("unavailable", "opencode server did not start")
            self._server, self._url = proc, found[0]
            return self._url

    def _reap(self) -> None:
        proc, self._server, self._url = self._server, None, None
        if proc is not None:
            if proc.poll() is None:
                _terminate_group(proc.pid)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    def stop(self) -> None:
        with self._lock:
            proc = self._server
            if proc is not None and proc.poll() is None:
                try:
                    proc.send_signal(signal.SIGTERM)  # graceful; group escalates after 1 s
                except OSError:
                    pass
            self._reap()

    # -- calls ---------------------------------------------------------------
    def _argv(self, url: str) -> list[str]:
        argv = [self.binary, "run", "--attach", url, "--format", "json", "--pure"]
        if self.model:
            argv += ["-m", self.model]
        return argv

    def delete_session(self, session_id: str) -> None:
        try:
            self.runner.run([self.binary, "session", "delete", session_id], "", 5.0, env=self._env)
        except BackendError:
            pass  # best effort; a leaked empty session holds no text

    def _events_for(self, system: str, user: str, timeout_s: float) -> Iterator[dict]:
        """Run one message; yields events, always deletes the session afterwards."""
        url = self.ensure_server()
        stdin = f"<system>\n{system}\n</system>\n\n{user}"
        sid: str | None = None
        try:
            for ev in _events(self.runner.stream(self._argv(url), stdin, timeout_s, env=self._env)):
                sid = ev.get("sessionID") or sid
                if ev.get("type") == "error":
                    err = ev.get("error") or {}
                    text = json.dumps(err)
                    kind = classify_error(text)
                    self._failure = "server" if _SERVER_BUG.search(text) else kind
                    raise BackendError(kind, error_detail(text, str(err.get("name") or "opencode_error")))
                yield ev
            self._failure = None
        finally:
            if sid:
                self.delete_session(sid)

    @staticmethod
    def _text(ev: dict) -> str:
        if ev.get("type") != "text":
            return ""
        part = ev.get("part")
        text = part.get("text") if isinstance(part, dict) else ev.get("text")
        return text if isinstance(text, str) else ""

    def _run_json(self, system: str, user: str, schema: dict, timeout_s: float) -> dict:
        text = "".join(self._text(ev) for ev in self._events_for(system, user, timeout_s))
        return extract_json_object(text)

    def _run_stream(self, system: str, user: str, timeout_s: float) -> Iterator[str]:
        got = False
        for ev in self._events_for(system, user, timeout_s):
            text = self._text(ev)
            if text:
                got = True
                yield text
        if not got:
            raise BackendError("bad_output", "no text events")

    def cancel(self) -> None:
        self.runner.cancel()

    def health(self) -> Health:
        try:
            res = self.runner.run([self.binary, "auth", "list"], "", 5.0, env=self._env)
        except BackendError as e:
            if e.kind == "unavailable":
                return Health(False, None, "opencode CLI not installed")
            return Health(True, None, e.kind)
        if self._failure == "server":
            return Health(True, False, UPDATE_HINT)
        m = _CREDS.search(_ANSI.sub("", res.stdout))
        if res.returncode != 0 or m is None:
            return Health(True, None, "unrecognised auth list output")
        if int(m.group(1)) >= 1:
            return Health(True, True, f"{m.group(1)} credential(s) ({EXPERIMENTAL})")
        return Health(True, False, "no credentials: run `opencode auth login`")
