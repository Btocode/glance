"""ShellHotkeys: the HotkeyPort over Glance's GNOME Shell extension (Wayland).

Wayland gives ordinary clients no global key grabs; the extension binds the chords inside
the shell and reports each press with `Key(name)` (`on_key`). Configured hotkeys become
keybindings on the extension's own GSettings keys, transient ones (Escape, the use key, Tab)
accelerators grabbed only while asked for.

While the extension is not on the bus a grab is remembered and reported as held, so glanced
does not tell the user about conflicts that are not there; `resync` grabs everything again
when the extension appears.
"""
from __future__ import annotations

from collections.abc import Callable

from glance.log import log_event
from glance.shellbus import ShellBus, ShellUnavailable
from glance.ui.hotkeys import parse_chord

_ACCEL_MODS = {"super": "<Super>", "alt": "<Alt>", "ctrl": "<Control>", "shift": "<Shift>"}
_ORDER = ("ctrl", "shift", "alt", "super")
# X keysym names that GTK's accelerator parser spells differently
_KEYS = {"enter": "Return", "esc": "Escape"}


def to_accel(chord: str) -> str:
    """`"Super+Alt+space"` -> `"<Super><Alt>space"`. Raises ValueError like parse_chord."""
    mods, key = parse_chord(chord)
    key = _KEYS.get(key.casefold(), key)
    return "".join(_ACCEL_MODS[m] for m in _ORDER if m in mods) + key


class ShellHotkeys:
    def __init__(self, bus: ShellBus, post: Callable[[Callable[[], None]], None]) -> None:
        self._bus = bus
        self._post = post
        self._wanted: dict[str, tuple[str, Callable[[], None]]] = {}
        self._held: set[str] = set()

    def grab(self, name: str, chord: str, cb: Callable[[], None]) -> bool:
        accel = to_accel(chord)
        self._wanted[name] = (chord, cb)
        if not self._bus.available:
            return True  # held as soon as the extension is there (`resync`)
        return self._send(name, accel)

    def _send(self, name: str, accel: str) -> bool:
        try:
            answer = self._bus.call("Grab", {"name": name, "accel": accel})
        except ShellUnavailable:
            return True
        ok = bool(isinstance(answer, dict) and answer.get("ok"))
        if ok:
            self._held.add(name)
        else:
            self._held.discard(name)
            log_event("shell_grab_refused", name=name)
        return ok

    def ungrab(self, name: str) -> None:
        self._wanted.pop(name, None)
        self._held.discard(name)
        if self._bus.available:
            try:
                self._bus.call("Ungrab", {"name": name})
            except ShellUnavailable:
                pass

    def grabbed(self) -> dict[str, str]:
        return {name: chord for name, (chord, _cb) in self._wanted.items()
                if name in self._held or not self._bus.available}

    def resync(self) -> None:
        """The extension (re)appeared: grab every chord again."""
        self._held.clear()
        for name, (chord, _cb) in list(self._wanted.items()):
            self._send(name, to_accel(chord))

    def on_key(self, name: str) -> None:
        """A press the extension reported (main loop)."""
        wanted = self._wanted.get(name)
        if wanted is not None:
            self._post(wanted[1])

    def close(self) -> None:
        for name in list(self._wanted):
            self.ungrab(name)
