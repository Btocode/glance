"""Subprocess runner for CLI backends: own session, empty cwd, hard timeouts, cancel."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from glance.backends.base import BackendError
from glance.log import log_event
from glance.paths import runtime_dir

KILL_GRACE_S = 1.0


def empty_dir() -> Path:
    d = runtime_dir() / "empty"
    d.mkdir(mode=0o700, exist_ok=True)
    d.chmod(0o700)
    return d


@dataclass
class ProcResult:
    stdout: str
    stderr: str
    returncode: int


def _killpg(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _terminate_group(pgid: int) -> None:
    """SIGTERM the group now; SIGKILL whatever is left after the grace period."""
    _killpg(pgid, signal.SIGTERM)

    def escalate() -> None:
        deadline = time.monotonic() + KILL_GRACE_S
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError):
                return
            time.sleep(0.05)
        _killpg(pgid, signal.SIGKILL)

    threading.Thread(target=escalate, daemon=True).start()


class _Call:
    """One running child; records why it was stopped (timeout / cancelled)."""

    def __init__(self, proc: subprocess.Popen, timeout_s: float | None) -> None:
        self.proc = proc
        self.reason: str | None = None
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        if timeout_s is not None:
            self._timer = threading.Timer(timeout_s, self.stop, args=("timeout",))
            self._timer.daemon = True
            self._timer.start()

    def stop(self, reason: str) -> None:
        with self._lock:
            if self.reason is None:
                self.reason = reason
        _terminate_group(self.proc.pid)

    def done(self) -> None:
        if self._timer:
            self._timer.cancel()

    def check(self) -> None:
        if self.reason:
            raise BackendError(self.reason)


def _write_stdin(proc: subprocess.Popen, text: str) -> None:
    def write() -> None:
        try:
            proc.stdin.write(text)
            proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass

    threading.Thread(target=write, daemon=True).start()


try:  # loaded in the parent: the child between fork and exec must only make one call
    import ctypes

    _PRCTL = ctypes.CDLL(None, use_errno=True).prctl
except Exception:  # noqa: BLE001 - not Linux / no libc: idle processes just outlive a kill -9
    _PRCTL = None


def _die_with_parent() -> None:
    """Runs in the child before exec: SIGTERM it when the thread that started it is gone, so
    a kill -9 of glanced leaves no idle process behind (PR_SET_PDEATHSIG = 1). Only the one
    thread that spawns idle processes may exit last."""
    _PRCTL(1, signal.SIGTERM)


class IdleProc:
    """A started process waiting for its stdin; `feed` sends the request and streams stdout."""

    def __init__(self, runner: ProcRunner, proc: subprocess.Popen) -> None:
        self._runner, self._proc, self._used = runner, proc, False

    @property
    def alive(self) -> bool:
        return self._proc.poll() is None

    def feed(self, stdin: str, timeout_s: float = 20.0) -> Iterator[str]:
        if self._used:
            raise BackendError("unavailable", "idle process already used")
        self._used = True
        call = self._runner._register(_Call(self._proc, timeout_s))
        _write_stdin(self._proc, stdin)
        yield from self._runner._read_lines(call)

    def kill(self) -> None:
        if self._proc.poll() is None:
            _terminate_group(self._proc.pid)
        try:
            self._proc.wait(timeout=KILL_GRACE_S + 1)
        except subprocess.TimeoutExpired:
            pass
        for f in (self._proc.stdin, self._proc.stdout, self._proc.stderr):
            try:
                f and f.close()
            except OSError:
                pass


class ProcRunner:
    def __init__(self, env: dict | None = None) -> None:
        self._env = env
        self._lock = threading.Lock()
        self._calls: set[_Call] = set()

    @property
    def env(self) -> dict:
        """Extra environment given at construction (for backends that spawn their own server)."""
        return dict(self._env or {})

    # -- internals ---------------------------------------------------------
    def _popen(self, argv: list[str], env: dict | None = None,
               die_with_parent: bool = False) -> subprocess.Popen:
        extra = {**(self._env or {}), **(env or {})}
        env = {**os.environ, **extra} if extra else None
        try:
            return subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", cwd=empty_dir(), env=env, start_new_session=True,
                preexec_fn=_die_with_parent if die_with_parent and _PRCTL else None,  # noqa: PLW1509
            )
        except (FileNotFoundError, PermissionError) as e:
            raise BackendError("unavailable", type(e).__name__) from e

    def _register(self, call: _Call) -> _Call:
        with self._lock:
            self._calls.add(call)
        return call

    def _finish(self, call: _Call) -> None:
        call.done()
        with self._lock:
            self._calls.discard(call)

    def _read_lines(self, call: _Call, err_sink: list | None = None) -> Iterator[str]:
        proc = call.proc
        err_len = [0]

        def drain() -> None:
            try:
                err = proc.stderr.read()
                err_len[0] = len(err)
                if err_sink is not None:
                    err_sink.append(err)  # caller classifies it; never logged
            except (OSError, ValueError):
                pass

        t = threading.Thread(target=drain, daemon=True)
        t.start()
        try:
            yield from proc.stdout
            proc.wait()
            t.join(1)
            call.check()
            if err_len[0]:
                log_event("proc_stderr", len=err_len[0])
        finally:
            if proc.poll() is None:
                call.stop("cancelled")
                proc.wait()
            call.done()
            self._finish(call)
            for f in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    f.close()
                except OSError:
                    pass

    # -- public API ----------------------------------------------------------
    def run(self, argv: list[str], stdin: str, timeout_s: float,
            env: dict | None = None) -> ProcResult:
        proc = self._popen(argv, env)
        call = self._register(_Call(proc, timeout_s))
        try:
            out, err = proc.communicate(stdin)
        except BaseException:
            call.stop("cancelled")
            raise
        finally:
            self._finish(call)
        call.check()
        if err:
            log_event("proc_stderr", len=len(err))
        return ProcResult(out, err, proc.returncode)

    def stream(self, argv: list[str], stdin: str, timeout_s: float,
               env: dict | None = None, err_sink: list | None = None) -> Iterator[str]:
        proc = self._popen(argv, env)
        call = self._register(_Call(proc, timeout_s))
        _write_stdin(proc, stdin)
        yield from self._read_lines(call, err_sink)

    def spawn_idle(self, argv: list[str]) -> IdleProc:
        return IdleProc(self, self._popen(argv, die_with_parent=True))

    def cancel(self) -> None:
        with self._lock:
            calls = list(self._calls)
        for c in calls:
            c.stop("cancelled")
