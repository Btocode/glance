"""XlibHotkeys: the HotkeyPort over XGrabKey on the root window (needs a live X session).

Facts from docs/superpowers/spike/findings.md S5.3/S5.4: a chord has to be grabbed once per
lock-key combination (NumLock, CapsLock) or it only fires with both off; a grab that another
client already holds fails with BadAccess; a grab on one of GNOME's own shortcuts succeeds and
takes it away, which is why `bind_hotkeys` checks those before it ever calls `grab`.
"""
from __future__ import annotations

import select
import threading
from collections.abc import Callable
from dataclasses import dataclass

from Xlib import XK, X, display, error

from glance.log import log_event
from glance.ui.hotkeys import parse_chord

_MASKS = {"shift": X.ShiftMask, "ctrl": X.ControlMask, "alt": X.Mod1Mask, "super": X.Mod4Mask}
_CHORD_MASK = X.ShiftMask | X.ControlMask | X.Mod1Mask | X.Mod4Mask
_POLL_S = 0.2


@dataclass(frozen=True)
class _Grab:
    chord: str
    keycode: int
    mask: int
    cb: Callable[[], None]


class XlibHotkeys:
    """Own X connection; key events are read on a thread and callbacks handed to `post`."""

    def __init__(self, post: Callable[[Callable[[], None]], None],
                 display_name: str | None = None) -> None:
        self._post = post
        self._d = display.Display(display_name)
        self._root = self._d.screen().root
        num = self._numlock_mask()
        self._locks = sorted({0, X.LockMask, num, X.LockMask | num})
        self._grabs: dict[str, _Grab] = {}
        self._lock = threading.Lock()
        # python-xlib's Display is not safe to use from two threads at once: a sync() on the
        # main loop while this thread reads an event can leave both waiting for ever.
        self._xlock = threading.RLock()
        self._last_release: tuple[int, int] | None = None  # (keycode, time): auto-repeat
        self._closing = threading.Event()
        self._thread = threading.Thread(target=self._run, name="glance-hotkeys", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ HotkeyPort

    def grab(self, name: str, chord: str, cb: Callable[[], None]) -> bool:
        mods, key = parse_chord(chord)
        keycode = self._keycode(key)
        mask = 0
        for m in mods:
            mask |= _MASKS[m]
        self.ungrab(name)
        with self._lock:
            if any((g.keycode, g.mask) == (keycode, mask) for g in self._grabs.values()):
                log_event("hotkey_conflict", name=name, holder="glance")
                return False
        denied = error.CatchError(error.BadAccess)
        with self._xlock:
            for lock in self._locks:
                self._root.grab_key(keycode, mask | lock, False, X.GrabModeAsync,
                                    X.GrabModeAsync, onerror=denied)
            self._d.sync()
        if denied.get_error() is not None:
            self._release(keycode, mask)  # the lock variants that did succeed
            log_event("hotkey_conflict", name=name, holder="other")
            return False
        with self._lock:
            self._grabs[name] = _Grab(chord, keycode, mask, cb)
        return True

    def ungrab(self, name: str) -> None:
        """Release `name`'s chord. Synchronous: the server has processed the ungrab when this
        returns, so a key pressed after it reaches the focused application (Tab, Ctrl+Right)."""
        with self._lock:
            g = self._grabs.pop(name, None)
        if g is not None:
            self._release(g.keycode, g.mask)

    def grabbed(self) -> dict[str, str]:
        with self._lock:
            return {name: g.chord for name, g in self._grabs.items()}

    def close(self) -> None:
        self._closing.set()
        self._thread.join(timeout=2.0)
        for name in list(self.grabbed()):
            self.ungrab(name)
        self._d.close()

    # ------------------------------------------------------------------ internals

    def _numlock_mask(self) -> int:
        code = self._d.keysym_to_keycode(XK.XK_Num_Lock)
        for index, codes in enumerate(self._d.get_modifier_mapping()):
            if code and code in codes:
                return 1 << index
        return X.Mod2Mask

    def _keycode(self, key: str) -> int:
        keysym = XK.string_to_keysym(key)
        if not keysym and len(key) == 1:
            keysym = XK.string_to_keysym(key.lower()) or ord(key.lower())
        with self._xlock:
            code = self._d.keysym_to_keycode(keysym) if keysym else 0
        if not code:
            raise ValueError(f"no key named {key!r} on this keyboard")
        return code

    def _release(self, keycode: int, mask: int) -> None:
        ignore = error.CatchError(error.BadAccess, error.BadValue)
        with self._xlock:
            for lock in self._locks:
                self._root.ungrab_key(keycode, mask | lock, onerror=ignore)
            self._d.sync()

    def _run(self) -> None:
        fd = self._d.fileno()
        while not self._closing.is_set():
            try:
                select.select([fd], [], [], _POLL_S)
                while not self._closing.is_set():
                    with self._xlock:
                        if not self._d.pending_events():
                            break
                        ev = self._d.next_event()
                        if getattr(ev, "type", None) == X.MappingNotify:
                            self._d.refresh_keyboard_mapping(ev)
                    self._on_event(ev)
            except (OSError, ValueError, error.ConnectionClosedError):
                if not self._closing.is_set():
                    log_event("hotkeys_connection_lost")
                return

    def _on_event(self, ev: object) -> None:
        kind = getattr(ev, "type", None)
        if kind == X.KeyRelease:
            self._last_release = (ev.detail, ev.time)
        elif kind == X.KeyPress:
            if self._last_release == (ev.detail, ev.time):
                return  # auto-repeat: the key is being held, fire once
            with self._lock:
                hit = next((g for g in self._grabs.values()
                            if (g.keycode, g.mask) == (ev.detail, ev.state & _CHORD_MASK)), None)
            if hit is not None:
                self._post(hit.cb)
