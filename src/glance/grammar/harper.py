"""Client for `harper-ls --stdio` (LSP over pipes).

Facts it relies on (Plan 0 findings S7.3-S7.5): ranges are UTF-16 `{line, character}`; the
diagnostic `code` is a linter key for rule lints but the replacement word for some spelling
lints; there is exactly one `publishDiagnostics` per `didOpen`/`didChange`/`didChangeConfiguration`
and it carries no version; settings are only read through the server's `workspace/configuration`
pull (answered with `[{"harper-ls": {...}}]`) plus `didChangeConfiguration`, replace wholesale,
and `userDictPath` is only applied at (re)start.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from glance.grammar.issues import Issue, utf16_to_cp

URI = "file:///glance/field.txt"
# A lint slower than `timeout_s` gets this much more before the check gives up: the first
# word far from every dictionary word costs harper-ls ~1 s once (it builds a structure it
# then keeps, ~38 MB, so it is not built ahead). A check that gave up returned nothing, and
# nothing checked again until the text changed.
SLOW_LINT_S = 2.0
SPELL_RULE = "SpellCheck"
_RULE_KEY = re.compile(r"^[A-Z][A-Za-z0-9]*$")


def frame(msg: dict) -> bytes:
    body = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


class FrameReader:
    """Incremental `Content-Length` frame parser."""

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, data: bytes) -> list[dict]:
        self._buf += data
        out: list[dict] = []
        while True:
            head_end = self._buf.find(b"\r\n\r\n")
            if head_end < 0:
                return out
            length = None
            for line in self._buf[:head_end].split(b"\r\n"):
                name, _, value = line.partition(b":")
                if name.strip().lower() == b"content-length":
                    length = int(value)
            if length is None:
                raise ValueError("frame without Content-Length")
            start = head_end + 4
            if len(self._buf) < start + length:
                return out
            out.append(json.loads(self._buf[start:start + length].decode("utf-8")))
            self._buf = self._buf[start + length:]


def _replacements(actions: list[dict]) -> tuple[str, ...]:
    seen: list[str] = []
    for a in actions:
        if a.get("kind") != "quickfix" or "edit" not in a:
            continue
        for edits in (a["edit"].get("changes") or {}).values():
            for e in edits:
                new = e.get("newText")
                if isinstance(new, str) and new not in seen:
                    seen.append(new)
    return tuple(seen)


def diagnostics_to_issues(text: str, diagnostics: list[dict],
                          actions: dict[int, list[dict]]) -> list[Issue]:
    """`actions` maps the index in `diagnostics` to its code actions."""
    out: list[Issue] = []
    seen: set[tuple[int, int, str]] = set()
    for idx, d in enumerate(diagnostics):
        r = d["range"]
        start = utf16_to_cp(text, r["start"]["line"], r["start"]["character"])
        end = utf16_to_cp(text, r["end"]["line"], r["end"]["character"])
        repl = _replacements(actions.get(idx, []))
        code = d.get("code")
        # `code` is the replacement word for some spelling lints: not a rule key then.
        rule = code if isinstance(code, str) and _RULE_KEY.match(code) and code not in repl \
            else SPELL_RULE
        key = (start, end, str(code))
        if key in seen:
            continue
        seen.add(key)
        out.append(Issue(f"harper:{rule}:{start}:{end}", "harper", rule,
                         str(d.get("message", "")), start, end, repl,
                         len(repl) == 1 and rule != SPELL_RULE))
    return out


class HarperClient:
    def __init__(self, binary: Path, user_dict: Path, timeout_s: float = 0.5) -> None:
        self._binary = binary
        self._user_dict = user_dict
        self._timeout_s = timeout_s
        self._proc: subprocess.Popen | None = None
        self._lock = threading.RLock()  # serialises check/config/lifecycle
        self._wlock = threading.Lock()  # serialises writes to the child's stdin
        self._cond = threading.Condition()
        self._disabled: set[str] = set()
        self._responses: dict[int, dict] = {}
        self._diag: list[dict] = []
        self._received = 0  # publishDiagnostics seen
        self._expected = 0  # publishDiagnostics owed for what we sent
        self._next_id = 0
        self._doc_version = 1
        self.degraded = False

    @property
    def alive(self) -> bool:
        p = self._proc
        return p is not None and p.poll() is None

    # lifecycle

    def start(self) -> None:
        with self._lock:
            if self.alive:
                return
            self._responses.clear()
            self._diag = []
            self._received = self._expected = 0
            self._doc_version = 1
            self.degraded = False
            self._proc = subprocess.Popen(
                [str(self._binary), "--stdio"], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            threading.Thread(target=self._pump, args=(self._proc,), daemon=True).start()
            startup = max(self._timeout_s, 5.0)
            self._request("initialize", {
                "processId": os.getpid(), "rootUri": None,
                "capabilities": {
                    "workspace": {"configuration": True},
                    "textDocument": {"codeAction": {"codeActionLiteralSupport": {
                        "codeActionKind": {"valueSet": ["quickfix"]}}}}}},
                timeout=startup)
            self._notify("initialized", {})
            # Open an empty document so the server pulls its settings now, then let it settle.
            self._send_doc("textDocument/didOpen", "")
            self._wait_diagnostics(time.monotonic() + startup)
            self._settle()

    def stop(self) -> None:
        with self._lock:
            p, self._proc = self._proc, None
            if p is None:
                return
            if p.poll() is None:
                try:
                    self._request("shutdown", None, timeout=0.5, proc=p)
                    self._notify("exit", None, proc=p)
                except (OSError, TimeoutError):
                    pass
                try:
                    p.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    p.terminate()
                    try:
                        p.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        p.wait()
            for f in (p.stdin, p.stdout):
                try:
                    f.close()
                except OSError:
                    pass

    # public API

    def check(self, text: str) -> list[Issue]:
        with self._lock:
            if not self.alive:
                self.degraded = True
                return []
            deadline = time.monotonic() + self._timeout_s
            try:
                self._send_doc("textDocument/didChange", text)
                if not self._wait_diagnostics(deadline):
                    deadline = time.monotonic() + SLOW_LINT_S
                    if not self._wait_diagnostics(deadline):
                        self.degraded = True
                        return []
                diagnostics = list(self._diag)
                ids = {}
                for idx, d in enumerate(diagnostics):
                    ids[idx] = self._send_request("textDocument/codeAction", {
                        "textDocument": {"uri": URI}, "range": d["range"],
                        "context": {"diagnostics": [d]}})
                actions: dict[int, list[dict]] = {}
                for idx, rid in ids.items():
                    resp = self._wait_response(rid, deadline)
                    if resp is None:
                        self.degraded = True
                        return []
                    actions[idx] = resp.get("result") or []
            except OSError:
                self.degraded = True
                return []
            self.degraded = False
            return diagnostics_to_issues(text, diagnostics, actions)

    def set_disabled_rules(self, rules: set[str]) -> None:
        with self._lock:
            self._disabled = set(rules)
            if self.alive:
                try:
                    self._push_config()
                except OSError:
                    pass

    def reload_dictionary(self) -> None:
        """`userDictPath` is only read at start, so restart the server."""
        with self._lock:
            self.stop()
            self.start()

    # internals

    def _settings(self) -> dict:
        cfg: dict[str, Any] = {"linters": {r: False for r in sorted(self._disabled)}}
        if self._user_dict.exists():
            cfg["userDictPath"] = str(self._user_dict)
        return {"harper-ls": cfg}

    def _push_config(self) -> None:
        with self._cond:
            self._expected += 1
        self._notify("workspace/didChangeConfiguration", {"settings": self._settings()})
        self._wait_diagnostics(time.monotonic() + self._timeout_s)

    def _send_doc(self, method: str, text: str) -> None:
        with self._cond:
            self._expected += 1
        if method == "textDocument/didOpen":
            doc = {"uri": URI, "languageId": "plaintext", "version": 1, "text": text}
            self._notify(method, {"textDocument": doc})
        else:
            self._doc_version += 1
            self._notify(method, {"textDocument": {"uri": URI, "version": self._doc_version},
                                  "contentChanges": [{"text": text}]})

    def _write(self, msg: dict, proc: subprocess.Popen | None = None) -> None:
        p = proc or self._proc
        if p is None or p.stdin is None:
            raise BrokenPipeError("harper-ls is not running")
        with self._wlock:
            p.stdin.write(frame(msg))
            p.stdin.flush()

    def _notify(self, method: str, params: Any, proc: subprocess.Popen | None = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params}, proc)

    def _send_request(self, method: str, params: Any,
                      proc: subprocess.Popen | None = None) -> int:
        with self._cond:
            self._next_id += 1
            rid = self._next_id
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}, proc)
        return rid

    def _request(self, method: str, params: Any, timeout: float,
                 proc: subprocess.Popen | None = None) -> dict:
        rid = self._send_request(method, params, proc)
        resp = self._wait_response(rid, time.monotonic() + timeout)
        if resp is None:
            raise TimeoutError(method)
        return resp

    def _wait_response(self, rid: int, deadline: float) -> dict | None:
        with self._cond:
            while rid not in self._responses:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._cond.wait(left)
            return self._responses.pop(rid)

    def _wait_diagnostics(self, deadline: float) -> bool:
        with self._cond:
            while self._received < self._expected:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(left)
            return True

    def _settle(self, quiet: float = 0.15) -> None:
        """Wait until no more diagnostics arrive for `quiet` seconds (config pull republish)."""
        with self._cond:
            end = time.monotonic() + 1.0
            while time.monotonic() < end:
                n = self._received
                self._cond.wait(quiet)
                if self._received == n:
                    # anything the server sent unprompted is now accounted for
                    self._expected = max(self._expected, self._received)
                    return

    def _pump(self, proc: subprocess.Popen) -> None:
        reader = FrameReader()
        try:
            while True:
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                for msg in reader.feed(chunk):
                    self._dispatch(msg, proc)
        except (OSError, ValueError):
            pass
        finally:
            with self._cond:
                self._cond.notify_all()

    def _dispatch(self, msg: dict, proc: subprocess.Popen) -> None:
        method = msg.get("method")
        if method and "id" in msg:  # server -> client request
            if method == "workspace/configuration":
                n = len((msg.get("params") or {}).get("items") or [{}])
                result: Any = [self._settings() for _ in range(n)]
            else:
                result = None
            try:
                self._write({"jsonrpc": "2.0", "id": msg["id"], "result": result}, proc)
            except OSError:
                pass
        elif method == "textDocument/publishDiagnostics":
            with self._cond:
                self._diag = msg["params"].get("diagnostics", [])
                self._received += 1
                self._cond.notify_all()
        elif "id" in msg and not method:
            with self._cond:
                self._responses[msg["id"]] = msg
                self._cond.notify_all()
