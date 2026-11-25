"""Clock port: time, timers and posting to the main loop."""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol


class Handle(Protocol):
    def cancel(self) -> None: ...


class Clock(Protocol):
    def now(self) -> float: ...
    def call_later(self, ms: int, fn: Callable[[], None]) -> Handle: ...
    def post(self, fn: Callable[[], None]) -> None: ...


class _GLibHandle:
    def __init__(self, source_id: int) -> None:
        self._id: int | None = source_id

    def cancel(self) -> None:
        if self._id is not None:
            from gi.repository import GLib

            GLib.source_remove(self._id)
            self._id = None


class GLibClock:
    """Clock backed by the GLib main loop (gi imported lazily)."""

    def now(self) -> float:
        return time.monotonic()

    def call_later(self, ms: int, fn: Callable[[], None]) -> Handle:
        from gi.repository import GLib

        holder: list[_GLibHandle] = []

        def run() -> bool:
            holder[0]._id = None  # source is removed by returning False
            fn()
            return False

        holder.append(_GLibHandle(GLib.timeout_add(ms, run)))
        return holder[0]

    def post(self, fn: Callable[[], None]) -> None:
        from gi.repository import GLib

        def run() -> bool:
            fn()
            return False

        GLib.idle_add(run)
