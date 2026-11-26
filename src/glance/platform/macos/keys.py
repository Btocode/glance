"""MacKeys: the write-back KeyPort over Quartz keyboard events.

- Events come from a private event source, so the user's physically held modifiers never
  combine with them, and are posted to the pinned process (`CGEventPostToPid`): a key can
  never land in another application even if the focus moves between two checks.
- Before every single key the frontmost process and its focused window are compared with
  the pinned target, and the HID system state is asked whether a hardware key went down
  since typing started; either ends the typing with `KeysAborted`, the rest unsent.
- Text is typed with `CGEventKeyboardSetUnicodeString`, one character per event, so it does
  not depend on the keyboard layout.

Needs PyObjC (Quartz, ApplicationServices); imported only on a Mac, in glance-capture.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import ApplicationServices as AX
import Quartz as Q

from glance.platform.macos.ax_port import TIMEOUT_S, focused_app, window_token
from glance.platform.macos.keyplan import (
    MODIFIER_KEYCODES,
    MODIFIER_MASK,
    KeyStroke,
    plan_chord,
    unicode_chunks,
)
from glance.writeback.ports import KeysAborted

_HID = Q.kCGEventSourceStateHIDSystemState
_POLL_S = 0.01
# Marks our events, for anyone (an event tap, a test) who wants to tell them apart.
GLANCE_EVENT_TAG = 0x474C4E43  # "GLNC"


class MacKeys:
    def __init__(self, sleep: Callable[[float], None] = time.sleep) -> None:
        self._sleep = sleep
        self._source = Q.CGEventSourceCreate(Q.kCGEventSourceStatePrivate)
        self._system = AX.AXUIElementCreateSystemWide()
        AX.AXUIElementSetMessagingTimeout(self._system, TIMEOUT_S)

    # ------------------------------------------------------------------ KeyPort

    def focus_target(self) -> tuple[int, int]:
        got = focused_app(self._system)
        if got is None:
            return 0, 0
        pid, app = got
        err, win = AX.AXUIElementCopyAttributeValue(app, "AXFocusedWindow", None)
        if err != AX.kAXErrorSuccess or win is None:
            return 0, 0
        return window_token(win), pid

    def any_key_down(self) -> bool:
        if Q.CGEventSourceFlagsState(_HID) & MODIFIER_MASK:
            return True
        return any(Q.CGEventSourceKeyState(_HID, code) for code in range(128))

    def can_type(self, text: str) -> bool:
        # Unicode events type any character under any layout; control characters are refused
        # by the engine before this is asked.
        return "\n" not in text and "\r" not in text

    def type_text(self, text: str, delay_ms: int, window: int | None = None) -> None:
        target, pid = self.focus_target()
        if window is not None and target != window:
            raise KeysAborted("focus moved")
        if not pid:
            raise KeysAborted("no target")
        started = time.monotonic()
        for chunk in unicode_chunks(text):
            if self.focus_target() != (target, pid):
                raise KeysAborted("focus moved")
            if self._user_key_since(started):
                raise KeysAborted("user typed")
            self._post(pid, 0, 0, chunk)
            if delay_ms:
                self._sleep(delay_ms / 1000.0)

    def chord(self, *keys: str) -> None:
        stroke = plan_chord(*keys)
        _target, pid = self.focus_target()
        if not pid:
            raise KeysAborted("no target")
        self._post_stroke(pid, stroke)

    def wait_modifiers_released(self, timeout_ms: int = 1000) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000.0
        while self.any_key_down():
            if time.monotonic() >= deadline:
                return False
            self._sleep(_POLL_S)
        return True

    def close(self) -> None:
        pass

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _user_key_since(started: float) -> bool:
        """A hardware key went down after `started` (events posted to a pid are not HID
        events, so ours never count)."""
        since = Q.CGEventSourceSecondsSinceLastEventType(_HID, Q.kCGEventKeyDown)
        return since < time.monotonic() - started

    def _event(self, code: int, down: bool, flags: int, chars: str) -> Any:
        ev = Q.CGEventCreateKeyboardEvent(self._source, code, down)
        Q.CGEventSetFlags(ev, flags)
        if chars:
            Q.CGEventKeyboardSetUnicodeString(ev, len(chars.encode("utf-16-le")) // 2, chars)
        Q.CGEventSetIntegerValueField(ev, Q.kCGEventSourceUserData, GLANCE_EVENT_TAG)
        return ev

    def _post(self, pid: int, code: int, flags: int, chars: str) -> None:
        Q.CGEventPostToPid(pid, self._event(code, True, flags, chars))
        Q.CGEventPostToPid(pid, self._event(code, False, flags, chars))

    def _post_stroke(self, pid: int, s: KeyStroke) -> None:
        # Key code and modifiers only: with a unicode string attached, AppKit did not always
        # take ⌘V / ⌘Z as the menu's key equivalent (seen on macOS 15.6).
        self._post(pid, s.keycode, s.flags, "")


def modifier_keycodes() -> tuple[int, ...]:
    return MODIFIER_KEYCODES
