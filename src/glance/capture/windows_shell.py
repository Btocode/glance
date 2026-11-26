"""ShellScreen: the focused window as Glance's GNOME Shell extension reports it (Wayland).

AtspiPort was written against Wnck, which does not work on Wayland. This module offers the
small part of Wnck's Screen/Window API that AtspiPort uses, fed by the extension's
`WindowChanged` signal (and one `FocusedWindow` call at start):

- `get_active_window()`, `get_windows_stacked()` and the `active-window-changed` signal;
- per window `get_pid()`, `get_class_group_name()` (the Wayland app id, or the X11 class of
  an XWayland window), `get_name()`, `get_geometry()` (the frame) and `geometry-changed`.

`origin()` is new: the top-left of the window's buffer, which AT-SPI WINDOW coordinates are
relative to (AT-SPI screen coordinates are meaningless on Wayland, where clients do not know
where they are). While the shell itself holds the keyboard (overview, a modal dialog) there
is no active window: nothing is captured and nothing typed.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from glance.log import log_event


class ShellWindow:
    def __init__(self, info: dict[str, Any]) -> None:
        self.id = str(info.get("id", ""))
        self._handlers: dict[int, tuple[str, Callable[..., Any]]] = {}
        self._next = 1
        self.update(info)

    def update(self, info: dict[str, Any]) -> bool:
        """Take the new record; True when the frame or buffer moved or changed size."""
        def rect(key: str) -> tuple[int, int, int, int]:
            try:
                x, y, w, h = (int(v) for v in info.get(key) or (0, 0, 0, 0))
            except (TypeError, ValueError):
                return (0, 0, 0, 0)
            return (x, y, w, h)

        old = getattr(self, "_frame", None), getattr(self, "_buffer", None)
        self._pid = int(info.get("pid") or 0)
        self._class = str(info.get("wm_class") or "")
        self._instance = str(info.get("wm_class_instance") or "")
        self._title = str(info.get("title") or "")
        self._frame = rect("frame")
        self._buffer = rect("buffer")
        return old != (self._frame, self._buffer)

    # the Wnck.Window surface AtspiPort uses

    def get_pid(self) -> int:
        return self._pid

    def get_class_group_name(self) -> str:
        return self._class

    def get_class_instance_name(self) -> str:
        return self._instance

    def get_name(self) -> str:
        return self._title

    def get_geometry(self) -> tuple[int, int, int, int]:
        return self._frame

    def origin(self) -> tuple[int, int]:
        """Where AT-SPI WINDOW coordinates (0, 0) is on the screen for most toolkits: the
        top-left of the window's buffer (its client-side shadow included)."""
        return self._buffer[0], self._buffer[1]

    def buffer_size(self) -> tuple[int, int]:
        return self._buffer[2], self._buffer[3]

    def frame_origin(self) -> tuple[int, int]:
        """The same for GTK 4, which measures from the window's frame."""
        return self._frame[0], self._frame[1]

    def connect(self, signal: str, cb: Callable[..., Any]) -> int:
        hid = self._next
        self._next += 1
        self._handlers[hid] = (signal, cb)
        return hid

    def disconnect(self, hid: int) -> None:
        self._handlers.pop(hid, None)

    def emit(self, signal: str) -> None:
        for sig, cb in list(self._handlers.values()):
            if sig == signal:
                cb(self)


class ShellScreen:
    """Wnck.Screen's surface for AtspiPort, over the extension. `bus` is a GioShellBus."""

    def __init__(self, bus: Any) -> None:
        self._bus = bus
        self._active: ShellWindow | None = None
        self._windows: dict[str, ShellWindow] = {}
        self._handlers: list[Callable[..., Any]] = []
        bus.subscribe("WindowChanged", self.on_state)
        try:
            self.on_state(bus.call("FocusedWindow"))
        except Exception as e:  # noqa: BLE001 - not there yet: the signal brings it
            log_event("shell_windows_unavailable", kind=type(e).__name__)

    def force_update(self) -> None:
        pass

    def get_active_window(self) -> ShellWindow | None:
        return self._active

    def get_windows_stacked(self) -> list[ShellWindow]:
        return [self._active] if self._active is not None else []

    def get_windows(self) -> list[ShellWindow]:
        return self.get_windows_stacked()

    def connect(self, signal: str, cb: Callable[..., Any]) -> int:
        if signal == "active-window-changed":
            self._handlers.append(cb)
        return len(self._handlers)

    def on_state(self, state: Any) -> None:
        """A WindowChanged signal (or the FocusedWindow answer)."""
        if not isinstance(state, dict):
            return
        info = state.get("focus")
        if state.get("shell_has_keyboard") or not isinstance(info, dict) or not info.get("id"):
            new = None
        else:
            new = self._windows.get(str(info["id"]))
            if new is None:
                new = ShellWindow(info)
                self._windows = {new.id: new}  # only the focused one is ever needed
                moved = False
            else:
                moved = new.update(info)
            if new is self._active and moved:
                new.emit("geometry-changed")
        if new is not self._active:
            previous, self._active = self._active, new
            for cb in list(self._handlers):
                cb(self, previous)
