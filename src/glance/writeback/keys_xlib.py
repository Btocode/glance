"""XlibKeys: the KeyPort over XTEST (needs a live X session).

Facts from docs/superpowers/spike/findings.md:

- S4.2/S4.3: XTEST typing loses no character at 0 ms per key; characters without a keycode
  in the current keymap are not typed at all (the engine pastes those).
- S4.8: while any key of a hotkey is still down, synthetic keys reach the application as
  shortcuts or go to the client holding the grab. `wait_modifiers_released` therefore waits
  for an all-zero XQueryKeymap, not only for the modifiers.
"""
from __future__ import annotations

import time

from Xlib import XK, X, display, error
from Xlib.ext import xtest

from glance.writeback.clipboard_xlib import same_client
from glance.writeback.ports import KeysAborted

_MODIFIERS = {"ctrl": "Control_L", "shift": "Shift_L", "alt": "Alt_L", "super": "Super_L"}
_POLL_S = 0.01
_GROUP_SHIFT = 13     # XKB puts the keyboard group in bits 13-14 of the core state


def char_keysym(ch: str) -> int | None:
    """The X keysym of a printable character; None for anything that must not be typed.

    Control characters are excluded on purpose: Return sends the message in a chat
    application and Tab moves the focus.
    """
    code = ord(ch)
    if 0x20 <= code <= 0x7E or 0xA0 <= code <= 0xFF:
        return code  # Latin-1 keysyms equal the code point
    if code < 0xA0 or 0xD800 <= code <= 0xDFFF:
        return None
    return 0x01000000 | code


class XlibKeys:
    def __init__(self) -> None:
        self._d = display.Display()
        if not self._d.has_extension("XTEST"):
            raise RuntimeError("the X server has no XTEST extension")
        self._root = self._d.screen().root
        self._net_active = self._d.intern_atom("_NET_ACTIVE_WINDOW")
        self._net_wm_pid = self._d.intern_atom("_NET_WM_PID")
        self._down: list[int] = []  # keycodes pressed and not yet released, in order

    def close(self) -> None:
        self.release_all()
        self._d.close()

    def release_all(self) -> None:
        """Release every key this process still holds down (on the way out): the X server
        keeps an XTEST key pressed after its client is gone, and every later key press on
        the desktop would arrive with it."""
        if not self._down:
            return
        for keycode in reversed(self._down):
            try:
                xtest.fake_input(self._d, X.KeyRelease, keycode)
            except Exception:  # noqa: BLE001, S110 - leaving regardless
                pass
        self._down.clear()
        try:
            self._d.sync()
        except Exception:  # noqa: BLE001, S110
            pass

    def _press(self, keycode: int) -> None:
        self._down.append(keycode)
        xtest.fake_input(self._d, X.KeyPress, keycode)

    def _release(self, keycode: int) -> None:
        xtest.fake_input(self._d, X.KeyRelease, keycode)
        if keycode in self._down:
            self._down.remove(keycode)

    # ------------------------------------------------------------------ KeyPort

    def focus_target(self) -> tuple[int, int]:
        window = self._focus_window()
        if not window:
            return 0, 0
        try:
            prop = self._d.create_resource_object("window", window).get_full_property(
                self._net_wm_pid, X.AnyPropertyType)
        except error.XError:
            return 0, 0  # the window went away between the two requests
        pid = int(prop.value[0]) if prop is not None and len(prop.value) else 0
        return (window, pid) if pid else (0, 0)

    def any_key_down(self) -> bool:
        return any(self._d.query_keymap())

    def can_type(self, text: str) -> bool:
        self._refresh_keymap()
        mask = self._root.query_pointer().mask
        if mask & X.LockMask or (mask >> _GROUP_SHIFT) & 3:
            # Caps Lock flips the case of what is typed; in a second layout group the same
            # keycodes produce other characters.
            return False
        return all(self._key(ch) is not None for ch in text)

    def type_text(self, text: str, delay_ms: int, window: int | None = None) -> None:
        plan = [self._key(ch) for ch in text]
        if any(k is None for k in plan):
            raise ValueError("text has a character outside the keymap")  # nothing typed yet
        if window is None:
            window = self._focus_window()
        shift = self._keycode("Shift_L")
        for keycode, shifted in plan:  # type: ignore[misc]
            if not window or self._focus_window() != window:  # before every single key
                raise KeysAborted("the target window lost the keyboard")
            try:
                if shifted:
                    self._press(shift)
                self._press(keycode)
            finally:
                for k in reversed(list(self._down)):  # whatever went down comes up again
                    self._release(k)
                self._d.sync()
            if delay_ms > 0:
                time.sleep(delay_ms / 1000)

    def chord(self, *keys: str) -> None:
        if not keys:
            raise ValueError("empty chord")
        held = [self._keycode(_MODIFIERS[name]) for name in keys[:-1]]
        key = self._keycode(keys[-1])
        try:
            for keycode in held:
                self._press(keycode)
            self._press(key)
        finally:
            # Whatever went down comes up again: a modifier left down corrupts all input.
            for keycode in reversed(list(self._down)):
                self._release(keycode)
            self._d.sync()

    def wait_modifiers_released(self, timeout_ms: int = 1000) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            if not any(self._d.query_keymap()):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(_POLL_S)

    # ------------------------------------------------------------------ helpers

    def _refresh_keymap(self) -> None:
        """Follow layout changes: python-xlib caches the mapping it read first."""
        while self._d.pending_events():
            event = self._d.next_event()
            if event.type == X.MappingNotify:
                self._d.refresh_keyboard_mapping(event)

    def _key(self, ch: str) -> tuple[int, bool] | None:
        """(keycode, needs Shift) for a character the first layout group has a key for."""
        keysym = char_keysym(ch)
        if keysym is None:
            return None
        for keycode, index in self._d.keysym_to_keycodes(keysym):
            if index in (0, 1):
                return keycode, index == 1
        return None

    def _keycode(self, keysym_name: str) -> int:
        keycode = self._d.keysym_to_keycode(XK.string_to_keysym(keysym_name))
        if not keycode:
            raise ValueError(f"no keycode for {keysym_name}")
        return keycode

    def _focus_window(self) -> int:
        """The active window, if the X input focus really is inside it; else 0.

        `_NET_ACTIVE_WINDOW` alone is not enough: while the shell's overview is open it can
        still name the application although keys go to the shell. The focus window must be
        a window of the same X client as the active one.
        """
        active = self._active_window()
        if not active:
            return 0
        focus = getattr(self._d.get_input_focus().focus, "id", 0)  # 0: None / PointerRoot
        mask = self._d.display.info.resource_id_mask
        return active if same_client(focus, active, mask) else 0

    def _active_window(self) -> int:
        prop = self._root.get_full_property(self._net_active, X.AnyPropertyType)
        return int(prop.value[0]) if prop is not None and len(prop.value) else 0
