"""MacSelectionWatch: "a selection settled" on macOS, without reading the selection.

An AX observer on the frontmost app takes `AXSelectedTextChanged` (it says the selection
moved, not what it holds), and a global monitor for the left mouse button (listen-only, no
keys) says whether the button is held and when it was let go. A selection made with the mouse
settles `SETTLE_MS` after the button is up; its length comes from `AXSelectedTextRange`
(offsets only) and its point is the pointer. A caret move (length 0), a selection made with
the keyboard, or a secure field: nothing is shown.

While the mark is up (`arm(True)`) only, a second global monitor reports key presses,
scrolling and clicks (the kind, never the key). Needs PyObjC and the Accessibility
permission glanced has already. Main thread only, apart from the observer callback, which
hands every notification to the main loop.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import ApplicationServices as AX
import objc
from AppKit import NSEvent, NSWorkspace

from glance.log import log_event
from glance.platform.macos import ax_roles
from glance.platform.macos.appkit_util import primary_height
from glance.platform.macos.ax_port import TIMEOUT_S, _opt, _range
from glance.platform.macos.geometry import point_from_cocoa
from glance.ui.selection_bar import SETTLE_MS

POLL_MS = 80
RETARGET_MS = 500          # which app is frontmost: checked this often while watching
RECENT_RELEASE_S = 0.6
_NOTE = "AXSelectedTextChanged"
# NSEventMask bits: left mouse down/up, key down, scroll wheel.
_LEFT_DOWN, _LEFT_UP, _KEY_DOWN, _SCROLL = 1 << 1, 1 << 2, 1 << 10, 1 << 22

_WATCHES: list[MacSelectionWatch] = []


@objc.callbackFor(AX.AXObserverCreate)
def _callback(observer: Any, element: Any, note: str, refcon: Any) -> None:
    for w in _WATCHES:
        w._post(w._changed)


class MacSelectionWatch:
    def __init__(self, call_later: Callable[[int, Callable[[], None]], Any],
                 now: Callable[[], float], post: Callable[[Callable[[], None]], None]) -> None:
        self._later, self._now, self._post = call_later, now, post
        self._on = False
        self._pid = 0
        self._observer: Any = None
        self._app: Any = None
        self._retarget: Any = None
        self._settling: Any = None
        self._mouse: Any = None
        self._armed: Any = None
        self._held = False
        self._released_at = -1e9
        self._changed_at = 0.0
        self._settled: Callable[..., None] = lambda *a, **k: None
        self._cleared: Callable[[], None] = lambda: None
        self._input: Callable[[str, int, int], None] = lambda k, x, y: None

    def start(self, on_settled: Callable[..., None], on_cleared: Callable[[], None],
              on_input: Callable[[str, int, int], None]) -> None:
        if self._on:
            return
        self._settled, self._cleared, self._input = on_settled, on_cleared, on_input
        self._on = True
        _WATCHES.append(self)
        self._mouse = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            _LEFT_DOWN | _LEFT_UP, self._mouse_event)
        self._target()

    def stop(self) -> None:
        if not self._on:
            return
        self._on = False
        self.arm(False)
        if self._mouse is not None:
            NSEvent.removeMonitor_(self._mouse)
            self._mouse = None
        for attr in ("_retarget", "_settling"):
            h = getattr(self, attr)
            if h is not None:
                h.cancel()
                setattr(self, attr, None)
        self._unobserve()
        if self in _WATCHES:
            _WATCHES.remove(self)

    def arm(self, on: bool) -> None:
        if on and self._armed is None and self._on:
            self._armed = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                _KEY_DOWN | _SCROLL | _LEFT_DOWN, self._armed_event)
        elif not on and self._armed is not None:
            NSEvent.removeMonitor_(self._armed)
            self._armed = None

    # ------------------------------------------------------------------ events

    def _mouse_event(self, event: Any) -> None:
        if event.type() == 1:      # NSEventTypeLeftMouseDown
            self._held = True
        elif event.type() == 2:    # NSEventTypeLeftMouseUp
            self._held = False
            self._released_at = self._now()

    def _armed_event(self, event: Any) -> None:
        kind = {10: "key", 22: "scroll", 1: "click"}.get(int(event.type()))
        if kind is not None:
            x, y = self._pointer()
            self._post(lambda: self._input(kind, x, y))

    def _changed(self) -> None:
        if not self._on:
            return
        now = self._now()
        if not (self._held or now - self._released_at <= RECENT_RELEASE_S):
            self._cleared()  # the keyboard (or typing) moved it
            return
        self._changed_at = now
        if self._settling is None:
            self._settling = self._later(POLL_MS, self._settle)

    def _settle(self) -> None:
        self._settling = None
        if not self._on:
            return
        if self._held or (self._now() - max(self._changed_at, self._released_at)) * 1000 \
                < SETTLE_MS:
            self._settling = self._later(POLL_MS, self._settle)
            return
        length = self._selected_length()
        if length is None or length <= 0:
            self._cleared()
            return
        x, y = self._pointer()
        self._settled(x, y, length)

    def _selected_length(self) -> int | None:
        """The focused element's selected range length: offsets, never the text; None in a
        secure field."""
        if self._app is None:
            return None
        el = _opt(self._app, "AXFocusedUIElement")
        if el is None or ax_roles.is_secure(_opt(el, "AXRole"), _opt(el, "AXSubrole")):
            return None
        r = _range(_opt(el, "AXSelectedTextRange"))
        return None if r is None else int(r[1])

    @staticmethod
    def _pointer() -> tuple[int, int]:
        p = NSEvent.mouseLocation()
        return point_from_cocoa(p.x, p.y, primary_height())

    # ------------------------------------------------------------------ the frontmost app

    def _target(self) -> None:
        self._retarget = None
        if not self._on:
            return
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        pid = int(app.processIdentifier()) if app is not None else 0
        if pid != self._pid:
            self._unobserve()
            self._observe(pid)
        self._retarget = self._later(RETARGET_MS, self._target)

    def _observe(self, pid: int) -> None:
        if not pid:
            return
        err, obs = AX.AXObserverCreate(pid, _callback, None)
        if err != AX.kAXErrorSuccess or obs is None:
            log_event("selection_watch_unavailable", err=int(err))
            return
        app = AX.AXUIElementCreateApplication(pid)
        AX.AXUIElementSetMessagingTimeout(app, TIMEOUT_S)
        AX.AXObserverAddNotification(obs, app, _NOTE, None)
        from CoreFoundation import CFRunLoopAddSource, CFRunLoopGetMain, kCFRunLoopDefaultMode

        CFRunLoopAddSource(CFRunLoopGetMain(), AX.AXObserverGetRunLoopSource(obs),
                           kCFRunLoopDefaultMode)
        self._pid, self._observer, self._app = pid, obs, app

    def _unobserve(self) -> None:
        if self._observer is not None:
            from CoreFoundation import (
                CFRunLoopGetMain,
                CFRunLoopRemoveSource,
                kCFRunLoopDefaultMode,
            )

            CFRunLoopRemoveSource(CFRunLoopGetMain(),
                                  AX.AXObserverGetRunLoopSource(self._observer),
                                  kCFRunLoopDefaultMode)
        self._pid, self._observer, self._app = 0, None, None
