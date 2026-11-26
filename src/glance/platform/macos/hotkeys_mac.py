"""Global hotkeys on macOS, the pure part: chords to Carbon codes, the macOS defaults, and the
system's own shortcuts. No Carbon calls here (`carbon_hotkeys.py` makes them).

Config keeps the Linux modifier names: `Super` is ⌘ Command, `Alt` ⌥ Option, `Ctrl` ⌃
Control, `Shift` ⇧. ⌘⌥ chords collide with application shortcuts everywhere, so a chord the
user left at its Linux default is replaced by the macOS default, a ⌃⌥ chord.
"""
from __future__ import annotations

import plistlib
import subprocess
from collections.abc import Mapping

from glance.config import _default_hotkeys
from glance.platform.macos.keyplan import KEYCODES, keycode
from glance.ui.hotkeys import Chord, parse_chord

# Carbon modifier bits (<HIToolbox/Events.h>).
CMD_KEY = 0x0100
SHIFT_KEY = 0x0200
OPTION_KEY = 0x0800
CONTROL_KEY = 0x1000
_CARBON = {"super": CMD_KEY, "shift": SHIFT_KEY, "alt": OPTION_KEY, "ctrl": CONTROL_KEY}

LINUX_DEFAULTS: dict[str, str] = _default_hotkeys()
# What a chord left at its Linux default is on a Mac. Every one was checked against macOS's
# own shortcuts (docs/shortcuts.md): ⌃⌥Space is "select next input source", so nothing sits
# there; ⌃⌥G (Glance) checks the field, ⌃⌥O opens the options (the panel).
MAC_DEFAULTS: dict[str, str] = {
    "check": "Ctrl+Alt+g", "panel": "Ctrl+Alt+o", "fix_all": "Ctrl+Alt+f",
    "revert": "Ctrl+Alt+z", "pause": "Ctrl+Alt+p", "tone1": "Ctrl+Alt+1",
    "tone2": "Ctrl+Alt+2", "tone3": "Ctrl+Alt+3", "tone4": "Ctrl+Alt+4",
    "complete": "Ctrl+Alt+Return", "selection": "Ctrl+Alt+a", "shot": "Ctrl+Alt+c",
}
_SYMBOLS = (("ctrl", "⌃"), ("alt", "⌥"), ("shift", "⇧"), ("super", "⌘"))
_KEY_SYMBOLS = {"return": "⏎", "space": "Space", "escape": "Esc", "tab": "⇥",
                "backspace": "⌫", "left": "←", "right": "→", "up": "↑", "down": "↓"}


# Transient keys the shared features grab under their Linux names, and what they are on a Mac:
# autocomplete takes one word with Ctrl+Right, which is "move a Space right" in macOS;
# ⌥→ is the Mac's own "one word right".
TRANSIENT: dict[str, str] = {"Control+Right": "Alt+Right", "Ctrl+Right": "Alt+Right"}


def mac_chord(chord: str) -> str:
    """The chord to register on a Mac for one a shared feature asked for."""
    return TRANSIENT.get(chord, chord)


def mac_hotkeys(chords: Mapping[str, str]) -> dict[str, str]:
    """The chords to bind on a Mac: each one still at its Linux default becomes the macOS
    default; whatever the user set stays as they wrote it."""
    return {name: (MAC_DEFAULTS.get(name, chord) if LINUX_DEFAULTS.get(name) == chord else chord)
            for name, chord in chords.items()}


def carbon_chord(chord: str) -> tuple[int, int]:
    """(virtual key code, Carbon modifiers) for a chord. Raises ValueError like `parse_chord`."""
    mods, key = parse_chord(chord)
    flags = 0
    for m in mods:
        flags |= _CARBON[m]
    return keycode(key), flags


def symbols(chord: str) -> str:
    """`Ctrl+Alt+Return` -> `⌃⌥⏎`, for menus and the strip's button."""
    try:
        mods, key = parse_chord(chord)
    except ValueError:
        return chord
    head = "".join(sym for name, sym in _SYMBOLS if name in mods)
    k = _KEY_SYMBOLS.get(key.casefold(), key.upper() if len(key) == 1 else key)
    return head + k


# ---------------------------------------------------------------------- system shortcuts

# NSEvent modifier flags as com.apple.symbolichotkeys stores them.
_NS_FLAGS = ((0x20000, "shift"), (0x40000, "ctrl"), (0x80000, "alt"), (0x100000, "super"))
_BY_CODE = {}
for _name, _code in KEYCODES.items():
    _BY_CODE.setdefault(_code, _name)


def parse_symbolic_hotkeys(data: bytes) -> set[Chord]:
    """The enabled shortcuts in a `com.apple.symbolichotkeys` plist, keys casefolded.

    Each entry is `{enabled, value: {parameters: [char, keycode, modifier flags]}}`; a keycode
    of 65535 means a character-only (unbound) entry and is skipped.
    """
    try:
        doc = plistlib.loads(data)
    except Exception:  # noqa: BLE001 - a plist we cannot read: nothing is reserved
        return set()
    out: set[Chord] = set()
    entries = doc.get("AppleSymbolicHotKeys", {}) if isinstance(doc, dict) else {}
    for entry in entries.values() if isinstance(entries, dict) else ():
        if not isinstance(entry, dict) or not entry.get("enabled"):
            continue
        params = (entry.get("value") or {}).get("parameters") or []
        if len(params) < 3:
            continue
        code, flags = int(params[1]), int(params[2])
        name = _BY_CODE.get(code)
        if code == 65535 or name is None:
            continue
        mods = frozenset(m for bit, m in _NS_FLAGS if flags & bit)
        out.add((mods, name.casefold()))
    return out


def mac_system_shortcuts() -> set[Chord]:
    """macOS's own enabled shortcuts, read with `defaults export` (read-only)."""
    try:
        done = subprocess.run(["defaults", "export", "com.apple.symbolichotkeys", "-"],
                              capture_output=True, timeout=2.0, check=False)
    except (OSError, subprocess.SubprocessError):
        return set()
    return parse_symbolic_hotkeys(done.stdout) if done.returncode == 0 else set()
