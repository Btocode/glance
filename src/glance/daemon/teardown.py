"""Leaving fast: glanced's exit never waits on a child, a backend or an AI call.

Quitting (tray, SIGTERM at logout, Ctrl+C) must take well under a second normally and
never more than DEADLINE_S: systemd otherwise holds the session ("A stop job is running for
Glance…"), and on Wayland every key glanced grabbed stays grabbed while it hangs around.

`Teardown.run` runs the steps in order, each one guarded (a failing step is logged, the
next one runs). A watchdog thread ends the process with `os._exit(0)` when the steps and
the worker threads together take longer than the deadline. `finish` then waits (within the
same deadline) for the threads the interpreter would otherwise join at exit, which an AI
call blocked for a minute in a pool worker would hold up; the ones still busy are left to
`os._exit`.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from glance.log import log_event

DEADLINE_S = 3.0


def _exit_now(code: int) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001, S110 - leaving regardless
            pass
    os._exit(code)


def blocking_threads() -> list[threading.Thread]:
    """Threads the interpreter joins before it exits (all non-daemon ones but this)."""
    me = threading.current_thread()
    return [t for t in threading.enumerate()
            if t is not me and t is not threading.main_thread() and not t.daemon
            and t.is_alive()]


def _wake_idle_workers() -> None:
    """What the interpreter does at exit for ThreadPoolExecutor, without its join: every
    idle pool worker ends now; a busy one ends after its job (or with os._exit)."""
    try:
        from concurrent.futures import thread as cft

        with cft._global_shutdown_lock:
            cft._shutdown = True
        for _t, q in list(cft._threads_queues.items()):
            q.put(None)
    except Exception as e:  # noqa: BLE001 - another Python: the deadline still holds
        log_event("teardown_wake_failed", kind=type(e).__name__)


class Teardown:
    def __init__(self, deadline_s: float = DEADLINE_S,
                 exit: Callable[[int], None] = _exit_now,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._deadline_s = deadline_s
        self._exit = exit
        self._clock = clock
        self._start = 0.0
        self._step = ""
        self._done = threading.Event()
        self.forced = False
        self._slowest = ("", 0)

    def run(self, steps: Sequence[tuple[str, Callable[[], Any]]]) -> None:
        self._start = self._clock()
        threading.Thread(target=self._watch, name="glance-teardown", daemon=True).start()
        slowest, slowest_ms = "", 0
        for name, fn in steps:
            if self.forced:
                return
            self._step = name
            began = self._clock()
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - leaving must not fail
                log_event("teardown_step_failed", step=name, kind=type(e).__name__)
            ms = int((self._clock() - began) * 1000)
            if ms > slowest_ms:
                slowest, slowest_ms = name, ms
        self._step = ""
        self._slowest = (slowest, slowest_ms)

    def finish(self, threads: Iterable[threading.Thread] | None = None) -> None:
        """Wait for `threads` (default: every thread that would block the exit) for what
        is left of the deadline; force the exit right away if any is still busy then."""
        if threads is None:
            _wake_idle_workers()
        busy = list(blocking_threads() if threads is None else threads)
        for t in busy:
            left = self._deadline_s - (self._clock() - self._start)
            if left <= 0:
                break
            t.join(left)
        still = [t for t in busy if t.is_alive()]
        if still and not self.forced:
            self._force("threads", busy=len(still))
        self._done.set()
        if not self.forced:
            log_event("glanced_stopped", ms=int((self._clock() - self._start) * 1000),
                      slowest=self._slowest[0], slowest_ms=self._slowest[1])

    def _watch(self) -> None:
        if not self._done.wait(self._deadline_s):
            self._force(self._step or "threads")

    def _force(self, step: str, **fields: object) -> None:
        if self.forced:
            return
        self.forced = True
        log_event("glanced_forced_exit", step=step,
                  after_ms=int((self._clock() - self._start) * 1000), **fields)
        self._exit(0)
