"""MacSelection: the SelectionPort for select-anything over AX.

macOS has no PRIMARY selection: the selection that matters is the focused element's
`AXSelectedText`. Its owner is therefore always the active window. A secure field's
selection is never read. Needs PyObjC; imported only on a Mac, in glanced (which then needs
the Accessibility permission as well, like glance-capture).
"""
from __future__ import annotations

from typing import Any

import ApplicationServices as AX
from AppKit import NSEvent, NSRunningApplication

from glance.platform.macos import ax_roles
from glance.platform.macos.appkit_util import primary_height
from glance.platform.macos.ax_port import TIMEOUT_S, _frame, _opt, focused_app, window_token
from glance.platform.macos.geometry import point_from_cocoa
from glance.ui.ports import WindowInfo


class MacSelection:
    def __init__(self) -> None:
        self._system = AX.AXUIElementCreateSystemWide()
        AX.AXUIElementSetMessagingTimeout(self._system, TIMEOUT_S)

    def _focus(self) -> tuple[int, Any, Any] | None:
        got = focused_app(self._system)
        if got is None:
            return None
        pid, app = got
        return pid, app, _opt(app, "AXFocusedWindow")

    def primary_text(self, max_bytes: int) -> str | None:
        got = self._focus()
        if got is None:
            return None
        el = _opt(got[1], "AXFocusedUIElement")
        if el is None or ax_roles.is_secure(_opt(el, "AXRole"), _opt(el, "AXSubrole")):
            return None
        text = _opt(el, "AXSelectedText")
        if not isinstance(text, str) or not text:
            return None
        if len(text.encode("utf-8")) > max_bytes:
            return None
        return str(text)

    def owner(self) -> WindowInfo | None:
        return self.active()

    def active(self) -> WindowInfo | None:
        got = self._focus()
        if got is None or got[2] is None:
            return None
        pid, _app, win = got
        running = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        bundle = str(running.bundleIdentifier() or "") if running is not None else ""
        title = _opt(win, "AXTitle")
        return WindowInfo(wm_class=bundle, title=str(title or ""), pid=pid,
                          xid=window_token(win), rect=_frame(win))

    def pointer(self) -> tuple[int, int]:
        p = NSEvent.mouseLocation()
        return point_from_cocoa(p.x, p.y, primary_height())
