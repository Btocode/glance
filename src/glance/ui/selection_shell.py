"""ShellSelection: the SelectionPort over Glance's GNOME Shell extension (Wayland).

The PRIMARY selection is read through the shell's clipboard (St.Clipboard). Wayland does not
say which client owns it: the owner is taken to be the focused window, which is where the
user selected the text with select-anything's hotkey just pressed. `primary_text` is called
on a worker thread; the Gio call is safe there.
"""
from __future__ import annotations

from typing import Any

from glance.protocol import Rect
from glance.shellbus import ShellBus, ShellUnavailable
from glance.ui.ports import WindowInfo


def window_info(raw: Any) -> WindowInfo | None:
    """A WindowInfo from the extension's window record; None when there is none."""
    if not isinstance(raw, dict):
        return None
    try:
        xid = int(raw.get("id") or 0)
        frame = raw.get("frame")
        rect = Rect(*(int(v) for v in frame)) if frame else None
        return WindowInfo(str(raw.get("wm_class") or ""), str(raw.get("title") or ""),
                          int(raw.get("pid") or 0), xid, rect)
    except (TypeError, ValueError):
        return None


class ShellSelection:
    def __init__(self, bus: ShellBus) -> None:
        self._bus = bus

    def primary_text(self, max_bytes: int) -> str | None:
        try:
            answer = self._bus.call("ReadPrimary", {"max_bytes": max_bytes})
        except ShellUnavailable:
            return None
        text = answer.get("text") if isinstance(answer, dict) else None
        if not isinstance(text, str) or not text:
            return None
        return text if len(text.encode("utf-8")) <= max_bytes else None

    def owner(self) -> WindowInfo | None:
        return self.active()

    def active(self) -> WindowInfo | None:
        try:
            state = self._bus.call("FocusedWindow")
        except ShellUnavailable:
            return None
        if not isinstance(state, dict) or state.get("shell_has_keyboard"):
            return None
        return window_info(state.get("focus"))

    def pointer(self) -> tuple[int, int]:
        try:
            x, y = self._bus.call("Pointer")
            return int(x), int(y)
        except (ShellUnavailable, TypeError, ValueError):
            return 0, 0


class ShellSelectionWatch:
    """SelectionWatchPort over the extension: it watches the shell's PRIMARY owner
    (`Meta.Selection` owner-changed: no content) and the pointer's button, and calls glanced
    with `SelectionSettled` {x, y} or `SelectionCleared`. Keys are not visible to it; the
    focused window changing (`on_window`) and the idle time hide the mark there."""

    def __init__(self, bus: ShellBus) -> None:
        self._bus = bus
        self._on: bool = False
        self._settled: Any = None
        self._cleared: Any = None
        self._input: Any = None

    def start(self, on_settled: Any, on_cleared: Any, on_input: Any) -> None:
        self._settled, self._cleared, self._input = on_settled, on_cleared, on_input
        self._on = True
        self._tell()

    def stop(self) -> None:
        self._on = False
        self._tell()

    def arm(self, on: bool) -> None:
        pass  # the extension cannot see the app's keys; the focus change and idle cover it

    def resync(self) -> None:
        """The extension (re)appeared: tell it again whether to watch."""
        self._tell()

    def _tell(self) -> None:
        try:
            self._bus.call("WatchSelection", {"on": self._on})
        except ShellUnavailable:
            pass

    # from the extension (ShellLink signals)
    def on_settled(self, payload: Any) -> None:
        if not self._on or self._settled is None or not isinstance(payload, dict):
            return
        try:
            x, y = int(payload.get("x", 0)), int(payload.get("y", 0))
        except (TypeError, ValueError):
            return
        self._settled(x, y)

    def on_cleared(self, _payload: Any = None) -> None:
        if self._on and self._cleared is not None:
            self._cleared()

    def on_window(self, _payload: Any = None) -> None:
        if self._on and self._input is not None:
            self._input("focus", 0, 0)
