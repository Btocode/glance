"""Keeps one child process running: restart with backoff, give up loudly, keep trying."""
from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import Any

from glance.clock import Clock, Handle
from glance.log import log_event

BACKOFF_MS = (200, 500, 1000, 2000)  # the last one repeats
STABLE_S = 60.0       # alive this long: the next death starts from the first delay again
FAIL_DEATHS = 5       # this many deaths within FAIL_WINDOW_S: `failed`
FAIL_WINDOW_S = 60.0
FAILED_RETRY_MS = 30_000
STOP_WAIT_S = 2.0


class ChildSupervisor:
    """`spawn` returns a Popen (or anything with poll / terminate / wait / kill).

    `on_state` gets `up`, `restarting` or `failed`, once per change. Call `poll()` every
    500 ms from the main loop; a spawn that raises counts as a child that died at once.
    """

    def __init__(self, name: str, spawn: Callable[[], Any], clock: Clock,
                 on_state: Callable[[str], None]) -> None:
        self._name = name
        self._spawn = spawn
        self._clock = clock
        self._on_state = on_state
        self._child: Any = None
        self._born = 0.0
        self._deaths: list[float] = []
        self._attempt = 0  # index into BACKOFF_MS for the next restart
        self._failed = False  # the slow lane; left only after STABLE_S alive
        self._timer: Handle | None = None
        self._running = False
        self.state: str | None = None

    @property
    def alive(self) -> bool:
        return self._child is not None and self._child.poll() is None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._launch()

    def stop(self) -> None:
        self._running = False
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        child, self._child = self._child, None
        if child is None or child.poll() is not None:
            return
        child.terminate()
        try:
            child.wait(timeout=STOP_WAIT_S)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()

    def poll(self) -> None:
        if not self._running or self._timer is not None:
            return  # stopped, or a restart is already scheduled
        if self.alive:
            if self._clock.now() - self._born >= STABLE_S:
                self._attempt = 0
                self._failed = False
            self._set_state("up")
            return
        self._died()

    # ------------------------------------------------------------------ internals

    def _launch(self) -> None:
        self._timer = None
        if not self._running:
            return
        try:
            self._child = self._spawn()
        except Exception as e:  # noqa: BLE001 - a missing binary must not stop the daemon
            log_event("child_spawn_failed", child=self._name, kind=type(e).__name__)
            self._child = None
            self._died()
            return
        self._born = self._clock.now()
        if not self._failed:  # in the slow lane only a child seen alive counts as up
            self._set_state("up")

    def _died(self) -> None:
        now = self._clock.now()
        self._child = None
        self._deaths = [t for t in self._deaths if now - t < FAIL_WINDOW_S] + [now]
        if len(self._deaths) >= FAIL_DEATHS:
            self._failed = True
        if self._failed:
            delay = FAILED_RETRY_MS
            self._set_state("failed")
        else:
            delay = BACKOFF_MS[min(self._attempt, len(BACKOFF_MS) - 1)]
            self._attempt += 1
            self._set_state("restarting")
        log_event("child_died", child=self._name, retry_ms=delay)
        self._timer = self._clock.call_later(delay, self._launch)

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            log_event("child_state", child=self._name, state=state)
            self._on_state(state)
