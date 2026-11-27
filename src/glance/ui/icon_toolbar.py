"""The status icon's toolbar: a slim horizontal bar of actions, the same component (and look)
as the selection action bar (`selection_bar.BarAction`). Pure; the three renderers (GTK on
X11, the GNOME Shell extension, macOS) draw exactly these items.

A right-click on the icon opens the toolbar (resting the pointer on it does not: `HOVER_OPENS`
is off, a hover menu got in the way of writing); it closes `CLOSE_MS` after the pointer has
left both the icon and the toolbar, or on Esc. A plain click on the icon is still the default
check.

    Check · Friendlier · Direct · Shorter · Professional · Rewrite… · ⋯

Check runs the check (the strip with the corrections). A tone runs that one rewrite and shows
it in the strip, the changes highlighted, for Alt+Enter to use; nothing is applied without it.
Only "Rewrite…" (the panel with its "Rewrite as…" input) and "⋯" open the Glance Assistant
panel. A muted icon (paused, private, no AI, limit reached) gets one line: the reason and its
single action.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from glance.protocol import Rect
from glance.ui.selection_bar import BarAction
from glance.ui.status_model import (
    AI_LIMIT_LINE,
    NO_AI_LINE,
    PAUSED_LINE,
    PRIVATE_LINE,
    StatusState,
    StatusView,
)

HOVER_OPENS = False  # resting the pointer on the icon opens nothing; a right-click does
OPEN_MS = 250  # (when HOVER_OPENS) how long the pointer rests on the icon first
CLOSE_MS = 400
TYPING_QUIET_MS = 600
GAP = 6  # between the caret line (or the icon) and the toolbar

# The panel action every toolbar item sends: {"key": item key}.
ACTION = "icon"
# The tones offered when the audience profile names none; at most `MAX_TONES` are shown.
DEFAULT_TONES = ("friendlier", "more_direct", "shorter", "professional")
MAX_TONES = 4
# Short labels for the toolbar (the panel's chips keep their long ones).
SHORT_LABELS = {"more_direct": "Direct", "add_structure": "Structure",
                "sharpen_prompt": "Sharpen"}


@dataclass(frozen=True)
class IconToolbar:
    """`actions` left to right; `note`: a muted state's one-line reason, drawn first;
    `hint`: the check shortcut ("Super+Alt+G"), a muted keycap after Check."""

    actions: tuple[BarAction, ...]
    note: str | None = None
    hint: str | None = None

    def item_keys(self) -> list[str]:
        return [a.key for a in self.actions]


def check_item(chord: str = "") -> BarAction:
    tip = "Check spelling & grammar" + (f" ({chord})" if chord else "")
    return BarAction("check", "Check", "check", tip)


REWRITE = BarAction("rewrite", "Rewrite…", "pen", "Rewrite as you say, in the Glance Assistant")
MORE = BarAction("more", "⋯", None, "Open the Glance Assistant")
RESUME = BarAction("resume", "Resume", None, "Start checking again")
SETUP = BarAction("setup", "Connect an AI…", None, "Set up an AI for tones and rewrites")


def tone_item(key: str, label: str | None = None) -> BarAction:
    from glance.backends.prompts import TONES
    from glance.ui.look import tone_glyph

    spec = TONES.get(key)
    shown = SHORT_LABELS.get(key) or label or (spec.label if spec else key)
    return BarAction(f"tone:{key}", shown, tone_glyph(key), f"Rewrite it {shown.lower()}: a preview in the strip")


def tone_key(item_key: str) -> str | None:
    """"tone:friendlier" -> "friendlier"; None for an item that is not a tone."""
    return item_key[5:] if item_key.startswith("tone:") and len(item_key) > 5 else None


def _tones(tones: tuple[str, ...]) -> tuple[str, ...]:
    from glance.backends.prompts import TONES

    picked = tuple(t for t in tones if t in TONES and t != "custom")[:MAX_TONES]
    return picked or DEFAULT_TONES


def icon_toolbar(status: StatusView, check_chord: str = "", ai: str = "ok",
                 tones: tuple[str, ...] = ()) -> IconToolbar | None:
    """The toolbar for the icon in `status` (None: no icon, nothing to offer). `ai` is the AI
    state (ok | none | limit | off); `check_chord` the check shortcut as shown to the user;
    `tones` the focused field's audience profile tones (the defaults when empty)."""
    state, message = status.state, status.message
    hint = check_chord or None
    if state is StatusState.HIDDEN:
        return None
    if message == PAUSED_LINE:
        return IconToolbar((RESUME,), note="Glance is paused.")
    if message == PRIVATE_LINE:
        return IconToolbar((), note=PRIVATE_LINE)
    if ai == "none" or message == NO_AI_LINE:
        return IconToolbar((SETUP,), note="No AI connected: spelling and grammar still work.")
    if ai == "limit" or message == AI_LIMIT_LINE:
        return IconToolbar((check_item(check_chord),), note="AI paused (limit reached).",
                           hint=hint)
    items = (check_item(check_chord), *(tone_item(t) for t in _tones(tones)), REWRITE, MORE)
    return IconToolbar(items, hint=hint)


def may_open(now: float, typed_at: float | None) -> bool:
    """Hover may open the toolbar: no key arrived within the last `TYPING_QUIET_MS`."""
    return typed_at is None or (now - typed_at) * 1000 >= TYPING_QUIET_MS - 1e-6


def toolbar_piece(tb: IconToolbar) -> dict[str, Any]:
    """The toolbar as JSON, for the GNOME Shell extension's scene."""
    return {"actions": [{"key": a.key, "label": a.label, "glyph": a.glyph, "tip": a.tip}
                        for a in tb.actions],
            "note": tb.note, "hint": tb.hint, "action": ACTION}


def place_toolbar(icon: Rect, line: Rect | None, size: tuple[int, int],
                  monitors: list[Rect]) -> tuple[int, int]:
    """Top-left of a toolbar of `size` for the icon at `icon` on the caret line `line`.

    Below the line, its right end under the icon's (it grows leftwards, along the line);
    above the line when there is no room below. Kept whole on the icon's monitor, and never
    over the caret line itself nor the icon.
    """
    w, h = size
    top = min(icon.y, line.y) if line is not None else icon.y
    bottom = max(icon.y + icon.h, line.y + line.h) if line is not None else icon.y + icon.h
    cx, cy = icon.x + icon.w // 2, icon.y + icon.h // 2
    mon = next((m for m in monitors if m.x <= cx < m.x + m.w and m.y <= cy < m.y + m.h),
               monitors[0] if monitors else None)
    x, y = icon.x + icon.w - w, bottom + GAP
    if mon is None:
        return max(0, x), y
    if y + h > mon.y + mon.h:
        y = top - GAP - h  # no room below: above the line
    x = max(mon.x, min(x, mon.x + mon.w - w))
    y = max(mon.y, min(y, mon.y + mon.h - h))
    return x, y
