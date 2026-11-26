"""Planning synthetic key events on macOS. Pure: no Quartz here.

The write-back engine speaks Linux key names (`chord("ctrl", "v")`, `chord("shift",
"Left")`). On a Mac the paste and undo shortcuts are on Command, so `ctrl` becomes ⌘ for
those; navigation keys keep their modifiers. Key codes are the ANSI virtual key codes of
`<HIToolbox/Events.h>`.
"""
from __future__ import annotations

from dataclasses import dataclass

# CGEventFlags
SHIFT = 0x20000
CONTROL = 0x40000
OPTION = 0x80000
COMMAND = 0x100000
MODIFIER_MASK = SHIFT | CONTROL | OPTION | COMMAND

KEYCODES: dict[str, int] = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11,
    "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21,
    "6": 22, "5": 23, "equal": 24, "9": 25, "7": 26, "minus": 27, "8": 28, "0": 29,
    "bracketright": 30, "o": 31, "u": 32, "bracketleft": 33, "i": 34, "p": 35, "Return": 36,
    "l": 37, "j": 38, "apostrophe": 39, "k": 40, "semicolon": 41, "backslash": 42, "comma": 43,
    "slash": 44, "n": 45, "m": 46, "period": 47, "Tab": 48, "space": 49, "grave": 50,
    "BackSpace": 51, "Escape": 53, "KP_Enter": 76, "F5": 96, "F6": 97, "F7": 98, "F3": 99,
    "F8": 100, "F9": 101, "F11": 103, "F10": 109, "F12": 111, "Home": 115, "Prior": 116,
    "Page_Up": 116, "Delete": 117, "F4": 118, "End": 119, "F2": 120, "Next": 121,
    "Page_Down": 121, "F1": 122, "Left": 123, "Right": 124, "Down": 125, "Up": 126,
}
KEYCODES["Enter"] = KEYCODES["Return"]
KEYCODES["Esc"] = KEYCODES["Escape"]
# Modifier keys themselves (for "is any key down").
MODIFIER_KEYCODES = (54, 55, 56, 57, 58, 59, 60, 61, 62, 63)

_FLAGS = {"shift": SHIFT, "ctrl": CONTROL, "alt": OPTION, "super": COMMAND}
# Linux shortcuts that are Command shortcuts on a Mac.
_COMMAND_KEYS = {"v", "z", "c", "x", "a", "y"}


@dataclass(frozen=True)
class KeyStroke:
    keycode: int
    flags: int
    chars: str  # the character the event carries (for ⌘ shortcuts under any layout)


def keycode(name: str) -> int:
    """The virtual key code for an X keysym-style name; letters in any case."""
    if name in KEYCODES:
        return KEYCODES[name]
    low = name.lower()
    if low in KEYCODES:
        return KEYCODES[low]
    for k, v in KEYCODES.items():
        if k.casefold() == name.casefold():
            return v
    raise ValueError(f"no key named {name!r}")


def plan_chord(*keys: str) -> KeyStroke:
    """One key press with modifiers held, as the engine names it, for macOS.

    `ctrl+v` → ⌘V and `ctrl+z` → ⌘Z (also c/x/a); everything else keeps its modifiers
    (`shift+Left` is ⇧←). The event carries the key's character so that ⌘V is paste under a
    Dvorak or AZERTY layout as well.
    """
    if not keys:
        raise ValueError("empty chord")
    *mods, key = keys
    flags = 0
    for m in mods:
        if m not in _FLAGS:
            raise ValueError(f"unknown modifier {m!r}")
        flags |= _FLAGS[m]
    low = key.lower()
    if flags & CONTROL and low in _COMMAND_KEYS and not flags & COMMAND:
        flags = (flags & ~CONTROL) | COMMAND
    if low == "y" and flags & COMMAND:  # redo is ⇧⌘Z on a Mac
        low, flags = "z", flags | SHIFT
    code = keycode(low if low in _COMMAND_KEYS or len(low) == 1 else key)
    chars = low if len(low) == 1 else ""
    return KeyStroke(code, flags, chars)


def unicode_chunks(text: str) -> list[str]:
    """Characters to type one per event: a surrogate pair or a base with its combining marks
    stays in one event, so the app gets whole characters (and the target is re-checked
    before each)."""
    import unicodedata

    out: list[str] = []
    for ch in text:
        if out and (unicodedata.combining(ch) or ch in "‍️︎"
                    or (out[-1].endswith("‍"))):
            out[-1] += ch
        else:
            out.append(ch)
    return out
