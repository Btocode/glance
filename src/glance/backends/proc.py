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

from glance.backends import cli_path
from glance.backends.base import BackendError
from glance.log import log_event
from glance.paths import runtime_dir

KILL_GRACE_S = 1.0
# A run whose answer is already in (the claude CLI's `result` line) is left to exit on its
# own: it lingers ~0.5-1 s after the answer, which nobody should wait for. Past this it is
# stopped like any other.
LINGER_S = 10.0


def empty_dir() -> Path:
    d = runtime_dir() / "empty"
    d.mkdir(mode=0o700, exist_ok=True)
    d.chmod(0o700)
    return d


def resolve_argv(argv: list[str]) -> list[str]:
    """argv with the program as an absolute path found through `cli_path` (the daemon's own
    PATH lacks ~/.local/bin, nvm...); BackendError("unavailable") when it is nowhere."""
    found = cli_path.find(argv[0])
    if found is None:
        raise BackendError("unavailable", "FileNotFoundError")
    return [found, *argv[1:]]


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
        self.linger = False  # the reader has its answer: let the process exit by itself
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


class Lines:
    """The stdout lines of one run. `release()`: the reader has what it needs; the process
    is left to finish by itself (reaped in the background, stopped after LINGER_S)."""

    def __init__(self, call: _Call, gen: Iterator[str]) -> None:
        self._call, self._gen = call, gen

    def __iter__(self) -> Lines:
        return self

    def __next__(self) -> str:
        return next(self._gen)

    def release(self) -> None:
        self._call.linger = True
        self._gen.close()

    def close(self) -> None:
        self._gen.close()


class IdleProc:
    """A started process waiting for its stdin; `feed` sends the request and streams stdout.

    `primer`: what was written to its stdin at once (the claude CLI gives up on a stdin that
    stays silent for 3 s; after the first byte it waits for the end). `feed` is then given
    the whole request, which must start with the primer, and writes the rest."""

    def __init__(self, runner: ProcRunner, proc: subprocess.Popen, argv: list[str] | None = None,
                 primer: str = "") -> None:
        self._runner, self._proc, self._used = runner, proc, False
        self.argv, self.primer = list(argv or []), primer
        self.touched = time.monotonic()

    @property
    def alive(self) -> bool:
        return self._proc.poll() is None

    @property
    def pid(self) -> int:
        return self._proc.pid

    def fits(self, argv: list[str], stdin: str) -> bool:
        """Started for exactly this argv, still waiting, and `stdin` starts with the primer."""
        return not self._used and self.alive and self.argv == list(argv) \
            and stdin.startswith(self.primer)

    def feed(self, stdin: str, timeout_s: float = 20.0) -> Lines:
        if self._used:
            raise BackendError("unavailable", "idle process already used")
        if not stdin.startswith(self.primer):
            raise BackendError("unavailable", "request does not start with the primer")
        self._used = True
        call = self._runner._register(_Call(self._proc, timeout_s))
        _write_stdin(self._proc, stdin[len(self.primer):])
        return Lines(call, self._runner._read_lines(call))

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


def _reap(proc: subprocess.Popen) -> None:
    """Let a released process finish: read what it still writes, wait LINGER_S at most."""
    def drain() -> None:
        try:
            for _ in proc.stdout:
                pass
        except (OSError, ValueError):
            pass

    t = threading.Thread(target=drain, daemon=True)
    t.start()
    try:
        proc.wait(timeout=LINGER_S)
    except subprocess.TimeoutExpired:
        _terminate_group(proc.pid)
        try:
            proc.wait(timeout=KILL_GRACE_S + 1)
        except subprocess.TimeoutExpired:
            pass
    t.join(1)
    for f in (proc.stdin, proc.stdout, proc.stderr):
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
        argv = resolve_argv(argv)
        env = {**os.environ, **cli_path.child_env(), **(self._env or {}), **(env or {})}
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
            if proc.poll() is None and call.linger and call.reason is None:
                self._finish(call)  # nothing to cancel any more
                threading.Thread(target=_reap, args=(proc,), daemon=True,
                                 name="proc-reap").start()
            else:
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
               env: dict | None = None, err_sink: list | None = None) -> Lines:
        proc = self._popen(argv, env)
        call = self._register(_Call(proc, timeout_s))
        _write_stdin(proc, stdin)
        return Lines(call, self._read_lines(call, err_sink))

    def spawn_idle(self, argv: list[str], primer: str = "") -> IdleProc:
        """A process started now, waiting for its request on stdin. `primer` is written at
        once (see IdleProc): it must hold nothing of the user's text."""
        proc = self._popen(argv, die_with_parent=True)
        if primer:
            try:
                proc.stdin.write(primer)
                proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass
        return IdleProc(self, proc, argv, primer)

    def cancel(self) -> None:
        with self._lock:
            calls = list(self._calls)
        for c in calls:
            c.stop("cancelled")
