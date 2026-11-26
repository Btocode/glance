"""ShellKeys: the KeyPort over Glance's GNOME Shell extension (Wayland).

The extension types through a Clutter virtual keyboard. Every key goes out only while the
pinned window (a Meta window id) is the focused window and the shell itself does not hold the
keyboard (overview, a modal dialog, a shell entry); otherwise the rest is not sent and
`KeysAborted` is raised here.

Mutter does not tell extensions which keys are physically down, only the modifier state:
`any_key_down` and `wait_modifiers_released` look at the modifiers. The engine's TypingWatch
still stops a write when the user types (the field's text changes).
"""
from __future__ import annotations

import time
from collections.abc import Callable

from glance.shellbus import ShellBus, ShellUnavailable
from glance.writeback.ports import KeysAborted

_POLL_S = 0.01
TYPE_TIMEOUT_MS = 5000


def typeable(text: str) -> bool:
    """Characters the virtual keyboard can type in any common layout: printable ASCII.
    Anything else is pasted (the engine decides)."""
    return all(0x20 <= ord(ch) <= 0x7E for ch in text)


class ShellKeys:
    def __init__(self, bus: ShellBus, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._bus = bus
        self._sleep = sleep
        self._clock = clock

    def close(self) -> None:
        pass

    def focus_target(self) -> tuple[int, int]:
        try:
            state = self._bus.call("FocusedWindow")
        except ShellUnavailable:
            return 0, 0
        if not isinstance(state, dict) or state.get("shell_has_keyboard"):
            return 0, 0
        focus = state.get("focus")
        if not isinstance(focus, dict):
            return 0, 0
        try:
            window, pid = int(focus.get("id") or 0), int(focus.get("pid") or 0)
        except (TypeError, ValueError):
            return 0, 0
        return (window, pid) if window and pid else (0, 0)

    def _key_state(self) -> dict:
        try:
            state = self._bus.call("KeyState")
        except ShellUnavailable:
            return {"down": True, "caps": True}  # unknown: nothing may be sent
        return state if isinstance(state, dict) else {"down": True, "caps": True}

    def any_key_down(self) -> bool:
        return bool(self._key_state().get("down"))

    def can_type(self, text: str) -> bool:
        if not typeable(text):
            return False
        return not self._key_state().get("caps")  # Caps Lock flips the case of what is typed

    def type_text(self, text: str, delay_ms: int, window: int | None = None) -> None:
        if not typeable(text):
            raise ValueError("text has a character the virtual keyboard does not type")
        if window is None:
            window = self.focus_target()[0]
        if not window:
            raise KeysAborted("no target window")
        try:
            answer = self._bus.call("TypeText", {"text": text, "window": str(window),
                                                 "delay_ms": max(0, int(delay_ms))},
                                    timeout_ms=TYPE_TIMEOUT_MS)
        except ShellUnavailable as e:
            raise KeysAborted("the shell did not answer") from e
        if not (isinstance(answer, dict) and answer.get("ok")):
            raise KeysAborted("the target window lost the keyboard")

    def chord(self, *keys: str) -> None:
        if not keys:
            raise ValueError("empty chord")
        try:
            answer = self._bus.call("Chord", {"keys": list(keys)})
        except ShellUnavailable as e:
            raise KeysAborted("the shell did not answer") from e
        if not (isinstance(answer, dict) and answer.get("ok")):
            if isinstance(answer, dict) and answer.get("reason") == "unknown key":
                raise ValueError(f"no key for {keys[-1]}")
            raise KeysAborted("the target window lost the keyboard")

    def wait_modifiers_released(self, timeout_ms: int = 1000) -> bool:
        deadline = self._clock() + timeout_ms / 1000
        while True:
            if not self.any_key_down():
                return True
            if self._clock() >= deadline:
                return False
            self._sleep(_POLL_S)
