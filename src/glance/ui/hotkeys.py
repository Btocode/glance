"""Global hotkeys: chord parsing, the port, and binding a set of chords to actions. No X here."""
from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Iterable, Mapping
from typing import Protocol

from glance.log import log_event

Chord = tuple[frozenset[str], str]

_MODIFIERS = {"super": "super", "alt": "alt", "ctrl": "ctrl", "control": "ctrl", "shift": "shift"}
# GNOME's accelerator spelling ("<Primary><Shift>q").
_ACCEL_MODIFIERS = {**_MODIFIERS, "primary": "ctrl", "mod1": "alt", "mod4": "super"}
_ACCEL = re.compile(r"'((?:<\w+>)*)(\w+)'")
# Shortcuts GNOME owns. A grab on one of them succeeds and silently takes it away from the
# desktop (findings S5.3: Super+Alt+S is the screen-reader toggle), so they are read first.
GNOME_SCHEMAS = (
    "org.gnome.desktop.wm.keybindings",
    "org.gnome.shell.keybindings",
    "org.gnome.mutter.keybindings",
    "org.gnome.settings-daemon.plugins.media-keys",
)


def parse_chord(s: str) -> Chord:
    """`"Super+Alt+space"` -> `({"super", "alt"}, "space")`; a bare key has no modifiers.

    Modifiers are super, alt, ctrl (or control) and shift, in any case. The key keeps its
    spelling: it is an X keysym name. Raises ValueError on an unknown modifier or empty key.
    """
    *mods, key = (part.strip() for part in s.split("+"))
    if not key:
        raise ValueError(f"hotkey {s!r} has no key")
    out = set()
    for m in mods:
        if m.casefold() not in _MODIFIERS:
            raise ValueError(f"hotkey {s!r}: unknown modifier {m!r}")
        out.add(_MODIFIERS[m.casefold()])
    return frozenset(out), key


class HotkeyPort(Protocol):
    def grab(self, name: str, chord: str, cb: Callable[[], None]) -> bool:
        """Grab `chord` under `name`; False when another program holds it.

        Raises ValueError for a chord that cannot be parsed or names no key.
        """
        ...

    def ungrab(self, name: str) -> None: ...

    def grabbed(self) -> dict[str, str]:
        """name -> chord for every grab held now."""
        ...


def bind_hotkeys(port: HotkeyPort, chords: Mapping[str, str],
                 actions: Mapping[str, Callable[[], None]],
                 system: Iterable[Chord] = (), overlay_key: bool = False) -> dict[str, str]:
    """Grab every chord in `chords` that has an action; never raises for a bad or taken one.

    Earlier grabs of these actions are released first, so this is also the reload path.
    Returns what could not be bound, name -> `invalid` (unparsable chord), `system` (the
    desktop's own shortcut: left alone), `conflict` (another program holds the grab) or
    `overlay` (`overlay_key`: GNOME opens the Activities overview when Super is let go, unless
    it saw another key; a grabbed Super+<key> hides that key from it, so the overview opens
    over whatever the shortcut showed).
    """
    reserved = {(mods, key.casefold()) for mods, key in system}
    for name in port.grabbed():
        if name in actions:
            port.ungrab(name)
    problems: dict[str, str] = {}
    for name, chord in chords.items():
        action = actions.get(name)
        if action is None:
            continue  # a hotkey of a feature that is not running
        try:
            mods, key = parse_chord(chord)
            if (mods, key.casefold()) in reserved:
                problems[name] = "system"
            elif overlay_key and mods == frozenset({"super"}):
                problems[name] = "overlay"
            elif not port.grab(name, chord, action):
                problems[name] = "conflict"
        except ValueError:
            problems[name] = "invalid"
    for name, problem in problems.items():
        log_event("hotkey_unavailable", name=name, problem=problem)
    return problems


def parse_accels(gsettings_output: str) -> set[Chord]:
    """The shortcuts in `gsettings list-recursively` output, keys casefolded."""
    out: set[Chord] = set()
    for mods, key in _ACCEL.findall(gsettings_output):
        names = [m.casefold() for m in re.findall(r"<(\w+)>", mods)]
        if all(n in _ACCEL_MODIFIERS for n in names):
            out.add((frozenset(_ACCEL_MODIFIERS[n] for n in names), key.casefold()))
    return out


def system_shortcuts() -> set[Chord]:
    """What GNOME binds, read with `gsettings` (read-only); empty when that is not possible."""
    out: set[Chord] = set()
    for schema in GNOME_SCHEMAS:
        try:
            done = subprocess.run(["gsettings", "list-recursively", schema],
                                  capture_output=True, text=True, timeout=2.0, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0:
            out |= parse_accels(done.stdout)
    return out
