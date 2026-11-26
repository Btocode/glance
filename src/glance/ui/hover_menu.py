"""The status icon's hover menu: what it offers in each state. Pure; the three renderers
(GTK on X11, the GNOME Shell extension, macOS) draw exactly these items.

The pointer resting on the icon for `OPEN_MS` opens it (never while the user is typing:
keys within the last `TYPING_QUIET_MS`); it closes `CLOSE_MS` after the pointer has left
both the icon and the menu. A plain click on the icon is still the default check.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from glance.ui.status_model import (
    AI_LIMIT_LINE,
    NO_AI_LINE,
    PAUSED_LINE,
    PRIVATE_LINE,
    StatusState,
    StatusView,
)

OPEN_MS = 250
CLOSE_MS = 400
TYPING_QUIET_MS = 600


@dataclass(frozen=True)
class MenuItem:
    """One row: `action` (+ `args`) goes through the same router as the strip's buttons."""

    label: str
    action: str
    args: dict[str, Any] = field(default_factory=dict)

    def __hash__(self) -> int:
        return hash((self.label, self.action, tuple(sorted(self.args.items()))))


@dataclass(frozen=True)
class HoverMenu:
    items: tuple[MenuItem, ...]
    note: str | None = None  # a muted state's one-line reason
    hint: str | None = None  # "Super+Alt+G to check": the check shortcut, muted


CHECK = MenuItem("Check spelling & grammar", "check")
CHECK_TONE = MenuItem("Check tone", "check_tone")
REWRITE = MenuItem("Rewrite…", "rewrite")
SHORTER = MenuItem("Shorter", "tone_apply", {"key": "shorter"})
PROFESSIONAL = MenuItem("More professional", "tone_apply", {"key": "professional"})
RESUME = MenuItem("Resume", "resume")
SETUP = MenuItem("Connect an AI…", "setup")


def hint_line(check_chord: str) -> str | None:
    """The muted line naming the check shortcut, e.g. "Super+Alt+G to check"."""
    return f"{check_chord} to check" if check_chord else None


def hover_menu(status: StatusView, check_chord: str = "", ai: str = "ok") -> HoverMenu | None:
    """The menu for the icon in `status` (None: no icon, nothing to offer). `ai` is the AI
    state (ok | none | limit | off); `check_chord` the check shortcut as shown to the user."""
    hint = hint_line(check_chord)
    state, message = status.state, status.message
    if state is StatusState.HIDDEN:
        return None
    if message == PAUSED_LINE:
        return HoverMenu((RESUME,), note=PAUSED_LINE)
    if message == PRIVATE_LINE:
        return HoverMenu((), note=PRIVATE_LINE)
    if ai == "none" or message == NO_AI_LINE:
        return HoverMenu((CHECK, SETUP), note=NO_AI_LINE, hint=hint)
    if ai == "limit" or message == AI_LIMIT_LINE:
        return HoverMenu((CHECK,), note=AI_LIMIT_LINE, hint=hint)
    return HoverMenu((CHECK, CHECK_TONE, REWRITE, SHORTER, PROFESSIONAL), hint=hint)


def may_open(now: float, typed_at: float | None) -> bool:
    """Hover may open the menu: no key arrived within the last `TYPING_QUIET_MS`."""
    return typed_at is None or (now - typed_at) * 1000 >= TYPING_QUIET_MS - 1e-6


def menu_piece(menu: HoverMenu) -> dict[str, Any]:
    """The menu as JSON, for the GNOME Shell extension's scene."""
    return {"items": [{"label": i.label, "action": i.action, "args": dict(i.args)}
                      for i in menu.items],
            "note": menu.note, "hint": menu.hint}
