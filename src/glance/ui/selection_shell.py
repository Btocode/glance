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
