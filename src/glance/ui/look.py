"""What the renderers draw beyond the views: keycap labels, the tone glyphs, the panel's
header line, the strip's markup. Pure; the GTK overlay, the GNOME Shell extension (through
the scene) and macOS all take these, so the three look alike.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from html import escape

from glance.ui.hotkeys import parse_chord
from glance.ui.strip_model import Segment

TITLE = "Glance Assistant"
SUGGESTED = "Suggested"
TONE = "Tone"
REWRITE_LEAD = "Rewrite as…"
RETURN_KEY = "↵"
ESC_KEY = "Esc"
CLOSE = "Close"
ACCEPT_KEY = "Tab"  # a completion after the caret: Tab takes it
FIX_ALL = "Fix All"

_ORDER = ("ctrl", "shift", "super", "alt")
_NAMES = {"ctrl": "Ctrl", "shift": "Shift", "super": "Super", "alt": "Alt"}
_MAC_MODS = {"ctrl": "⌃", "alt": "⌥", "shift": "⇧", "super": "⌘"}
_MAC_ORDER = ("ctrl", "alt", "shift", "super")
_KEYS = {"return": "Enter", "enter": "Enter", "kp_enter": "Enter", "space": "Space",
         "escape": "Esc", "tab": "Tab", "backspace": "Backspace", "left": "←", "right": "→",
         "up": "↑", "down": "↓"}
_MAC_KEYS = {**_KEYS, "return": "⏎", "enter": "⏎", "tab": "⇥", "backspace": "⌫"}


# The status icon: the brand mark alone (no disc, no ring), its glow and the count badge.
@dataclass(frozen=True)
class IconLook:
    mark_alpha: float  # the mark's opacity
    glow: float  # the glow's strength, times the alpha of the theme token `glow_token`
    grey: bool  # the mark as a flat grey silhouette (muted)
    pulse: bool  # the glow breathes (PULSE, one step per frame)
    glow_token: str = "icon_glow"  # the theme token the glow is drawn in
    # Working, animated: a thin arc around the mark starts at this angle (radians, 0 at
    # three o'clock, clockwise) and sweeps ARC_SWEEP. None: no arc (never a ring at rest).
    arc: float | None = None


ICON_LOOKS = {
    "idle": IconLook(0.7, 0.0, False, False),
    "working": IconLook(1.0, 1.0, False, True, "icon_working"),
    "ready": IconLook(1.0, 1.0, False, False),
    "muted": IconLook(0.55, 0.0, True, False),
}

# The working state's animation: FRAMES frames FRAME_MS apart (a 1.2 s cycle). The glow
# breathes between 0.35 and 0.75 opacity (`icon_working` is opaque, so these are its
# alpha) and the arc goes round once a cycle. Only while the icon spins: never while keys
# arrive (a still glow, no arc, costs nothing per keystroke).
FRAMES = 24
FRAME_MS = 50
PULSE = tuple(round(0.55 - 0.2 * math.cos(2 * math.pi * i / FRAMES), 3) for i in range(FRAMES))
STILL_GLOW = 0.55
ARC_SWEEP = 1.9  # radians: about a third of the circle
ARC_RADIUS = 10.5  # px from the mark's centre (the mark is 18 px)
ARC_WIDTH = 1.6


def icon_look(state: str, spin: bool = False, phase: int = 0) -> IconLook:
    """How the icon is drawn in `state` (StatusState's value). Working breathes and its arc
    turns only while `spin` (not while keys arrive); `phase` is the animation frame."""
    base = ICON_LOOKS.get(state, ICON_LOOKS["idle"])
    if state == "working":
        if not spin:
            return replace(base, glow=STILL_GLOW, pulse=False)
        i = phase % FRAMES
        return replace(base, glow=PULSE[i], pulse=True, arc=2 * math.pi * i / FRAMES)
    return base


# Loading states, the same in every renderer: the strip's loader line, and the skeleton
# bars that stand in for "Suggested" and the tone cards' lines while a check runs.
SKELETON_SUGGESTED = (1.0, 0.82, 0.55)  # bar widths, as a share of the box's width
SKELETON_CARD = (0.8,)
SKELETON_BAR_H = 10
SKELETON_GAP = 7


def chord_keys(chord: str, mac: bool = False) -> list[str]:
    """The keys of `chord` as keycaps show them: `Super+Alt+Return` -> Super, Alt, Enter
    (macOS: ⌃, ⌥, ⏎). A chord that does not parse is one keycap."""
    try:
        mods, key = parse_chord(chord)
    except ValueError:
        return [chord] if chord else []
    names = _MAC_KEYS if mac else _KEYS
    k = names.get(key.casefold(), key.upper() if len(key) == 1 else key.capitalize())
    if mac:
        return [*(_MAC_MODS[m] for m in _MAC_ORDER if m in mods), k]
    return [*(_NAMES[m] for m in _ORDER if m in mods), k]


def chord_text(chord: str, mac: bool = False) -> str:
    """One keycap's text for a chord: `Alt+Enter`, or `⌥⏎` on a Mac."""
    return ("" if mac else "+").join(chord_keys(chord, mac))


def apply_label(use_key: str, mac: bool = False) -> str:
    """The panel's primary button: "Apply (Alt+Enter)"."""
    return f"Apply ({chord_text(use_key, mac)})"


def fixes_label(n: int) -> str:
    return f"{n} fix{'' if n == 1 else 'es'}"


def subline(mode: str, has_tones: bool, origin: str | None = None) -> str:
    """The muted line under the panel's title: what is at work. `origin` "shot": a result
    about text a Glance Shot read; "shot_text": that text itself (read on this computer)."""
    if origin == "shot_text":
        return "Glance Shot · read on this computer"
    if origin == "shot":
        return "Glance Shot"
    head = "Selected text" if mode in ("selection", "result") else "Local grammar"
    return f"{head} · AI tones" if has_tones else head


CARD_HINT = "Click to preview"


def card_skeleton(view_loading: bool, preview: str | None, state: str | None) -> bool:
    """A tone card shows a skeleton bar for its line while the check runs, until it has a
    rewrite of its own (or is being rewritten)."""
    return view_loading and not preview and state is None


def card_line(preview: str | None, state: str | None) -> str:
    """A tone card's second line: its rewrite on one line once there is one, "Rewriting…"
    while it is being written, "Click to preview" before (nothing is asked for on hover)."""
    if state == "loading" and not preview:
        return "Rewriting…"
    if state == "failed":
        return "Did not work"
    if preview:
        return " ".join(preview.split())
    return CARD_HINT


def tone_line(tone: str | None, reason: str | None) -> str | None:
    """"Reads blunt — curt for a channel", or None."""
    if not tone:
        return None
    return f"Reads {tone.lower()}" + (f" — {reason}" if reason else "")


# Line glyphs in a 16 px box, drawn with a 1.5 px round stroke in the accent colour: M (move),
# L (line), C (cubic curve), Z (close). No emoji anywhere.
GLYPHS: dict[str, tuple[tuple, ...]] = {
    "heart": (("M", 8, 13.2), ("C", 8, 13.2, 2.4, 9.9, 2.4, 6.1),
              ("C", 2.4, 4.3, 3.8, 3, 5.4, 3), ("C", 6.6, 3, 7.5, 3.7, 8, 4.6),
              ("C", 8.5, 3.7, 9.4, 3, 10.6, 3), ("C", 12.2, 3, 13.6, 4.3, 13.6, 6.1),
              ("C", 13.6, 9.9, 8, 13.2, 8, 13.2), ("Z",)),
    "lines": (("M", 3, 4.5), ("L", 13, 4.5), ("M", 3, 8), ("L", 11, 8), ("M", 3, 11.5),
              ("L", 8, 11.5)),
    "bolt": (("M", 9.2, 2), ("L", 3.8, 9), ("L", 7.8, 9), ("L", 6.8, 14), ("L", 12.2, 7),
             ("L", 8.2, 7), ("Z",)),
    "briefcase": (("M", 3.5, 5.5), ("L", 12.5, 5.5), ("C", 13.1, 5.5, 13.5, 5.9, 13.5, 6.5),
                  ("L", 13.5, 12), ("C", 13.5, 12.6, 13.1, 13, 12.5, 13), ("L", 3.5, 13),
                  ("C", 2.9, 13, 2.5, 12.6, 2.5, 12), ("L", 2.5, 6.5),
                  ("C", 2.5, 5.9, 2.9, 5.5, 3.5, 5.5), ("Z",),
                  ("M", 6, 5.5), ("L", 6, 4), ("C", 6, 3.4, 6.4, 3, 7, 3), ("L", 9, 3),
                  ("C", 9.6, 3, 10, 3.4, 10, 4), ("L", 10, 5.5),
                  ("M", 2.5, 8.8), ("L", 13.5, 8.8)),
    "pen": (("M", 3, 13), ("L", 3.6, 10.2), ("L", 10.8, 3), ("L", 13, 5.2), ("L", 5.8, 12.4),
            ("Z",), ("M", 9.4, 4.4), ("L", 11.6, 6.6)),
    # The selection action bar's: a tick, a reply arrow, two sheets, a globe.
    "check": (("M", 3, 8.6), ("L", 6.4, 12), ("L", 13, 4.5)),
    "reply": (("M", 6.5, 4), ("L", 2.5, 8), ("L", 6.5, 12), ("M", 2.5, 8), ("L", 9.5, 8),
              ("C", 11.9, 8, 13.5, 9.6, 13.5, 12), ("L", 13.5, 13)),
    "copy": (("M", 6, 5.5), ("L", 12.5, 5.5), ("L", 12.5, 13.5), ("L", 6, 13.5), ("Z",),
             ("M", 3.5, 10.5), ("L", 3.5, 2.5), ("L", 10, 2.5)),
    "globe": (("M", 8, 2.5), ("C", 11.04, 2.5, 13.5, 4.96, 13.5, 8),
              ("C", 13.5, 11.04, 11.04, 13.5, 8, 13.5), ("C", 4.96, 13.5, 2.5, 11.04, 2.5, 8),
              ("C", 2.5, 4.96, 4.96, 2.5, 8, 2.5), ("Z",),
              ("M", 8, 2.5), ("C", 5.8, 4.6, 5.8, 11.4, 8, 13.5),
              ("M", 8, 2.5), ("C", 10.2, 4.6, 10.2, 11.4, 8, 13.5), ("M", 2.5, 8), ("L", 13.5, 8)),
    "spark": (("M", 8, 2.5), ("C", 8.6, 6, 10, 7.4, 13.5, 8), ("C", 10, 8.6, 8.6, 10, 8, 13.5),
              ("C", 7.4, 10, 6, 8.6, 2.5, 8), ("C", 6, 7.4, 7.4, 6, 8, 2.5), ("Z",)),
}

_TONE_GLYPHS = (
    (("friend", "warm", "casual", "kind", "polite", "empath"), "heart"),
    (("short", "concise", "brief", "simpl", "clear", "trim", "structure"), "lines"),
    (("direct", "confident", "assert", "bold", "urgent", "action"), "bolt"),
    (("professional", "formal", "business", "neutral"), "briefcase"),
    (("custom",), "pen"),
)


def tone_glyph(key: str) -> str:
    """The glyph for a tone chip: a heart for friendly, lines for concise, a bolt for direct,
    a briefcase for professional; a spark for any other tone an audience profile adds."""
    k = key.casefold()
    for words, glyph in _TONE_GLYPHS:
        if any(w in k for w in words):
            return glyph
    return "spark"


def strip_markup(segments: tuple[Segment, ...], accent: str) -> str:
    """The strip's sentence as Pango markup: the fixes in the emerald ink, semibold."""
    return "".join(
        f'<span foreground="{accent}" weight="600">{escape(s.text, quote=False)}</span>'
        if s.changed else escape(s.text, quote=False) for s in segments)


def suggested_markup(segments: tuple[Segment, ...], tokens: dict[str, str]) -> str:
    """The panel's suggested text as Pango markup: every changed word on the emerald tint."""
    return "".join(
        f'<span background="{tokens["highlight"]}" foreground="{tokens["text"]}">'
        f'{escape(s.text, quote=False)}</span>'
        if s.changed else escape(s.text, quote=False) for s in segments)
