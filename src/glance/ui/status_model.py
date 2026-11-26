"""Pure model of the status icon: the small round Glance mark next to the focused field.

It says, quietly, that Glance is on this field and what it is doing. No GTK, no text: the
inputs are flags and counts, the output a state, a count and (for the muted states) the one
line the strip shows when the icon is clicked.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class StatusState(Enum):
    HIDDEN = "hidden"    # no writable field, or Glance has nothing to do with it
    IDLE = "idle"        # watching this field, nothing to show
    WORKING = "working"  # a local check is pending, or an AI analysis/rewrite is in flight
    READY = "ready"      # the strip has something: `count` fixes, or a tone/guard line
    MUTED = "muted"      # paused, a private field, no AI connected / AI limit reached


# What a click on a muted icon shows in the strip's place (never the field's text).
PAUSED_LINE = "Glance is paused. Resume it from the tray menu."
PRIVATE_LINE = "Private field: Glance reads nothing here."
NO_AI_LINE = "No AI connected: spelling and grammar fixes still work. Set one up from the tray."
AI_LIMIT_LINE = "AI paused for now (limit reached): spelling and grammar fixes still work."
CHECKING_LINE = "Checking…"
NOTHING_LINE = "Nothing to fix here."


@dataclass(frozen=True)
class StatusInputs:
    has_field: bool = False  # a writable field is focused (and capture is connected)
    paused: bool = False
    gated: bool = False      # a private field (denylisted app or page, password, incognito)
    checking: bool = False   # the text changed and its local check has not come back yet
    typing: bool = False     # keys are still arriving (the text has not settled)
    analyzing: bool = False  # an AI analysis or rewrite for this field is in flight
    n_fixes: int = 0         # fixes the strip would apply now
    has_note: bool = False   # the strip has a tone note or send-guard line (no fix)
    ai: str = "off"          # off | ok | none | limit


@dataclass(frozen=True)
class StatusView:
    state: StatusState
    count: int = 0            # READY: the number on the badge (0: a dot, tone-only)
    message: str | None = None  # MUTED: the line a click shows
    # WORKING: the ring turns. Not while keys keep arriving (a still ring says the same and
    # costs nothing per keystroke); it turns once the user pauses and work is still going.
    spin: bool = False

    @property
    def badge(self) -> str | None:
        """What the badge shows: the fix count, "•" for a tone/guard line, None for none."""
        if self.state is not StatusState.READY:
            return None
        if self.count <= 0:
            return "•"
        return str(self.count) if self.count < 10 else "9+"


def status_view(i: StatusInputs) -> StatusView:
    """Paused and private win; then work in progress; then what the strip has; then the
    quiet AI states; otherwise idle. Local fixes still count as ready without an AI."""
    if not i.has_field:
        return StatusView(StatusState.HIDDEN)
    if i.paused:
        return StatusView(StatusState.MUTED, message=PAUSED_LINE)
    if i.gated:
        return StatusView(StatusState.MUTED, message=PRIVATE_LINE)
    if i.checking or i.analyzing:
        return StatusView(StatusState.WORKING, spin=i.analyzing or not i.typing)
    if i.n_fixes > 0:
        return StatusView(StatusState.READY, count=i.n_fixes)
    if i.has_note:
        return StatusView(StatusState.READY)
    if i.ai == "none":
        return StatusView(StatusState.MUTED, message=NO_AI_LINE)
    if i.ai == "limit":
        return StatusView(StatusState.MUTED, message=AI_LIMIT_LINE)
    return StatusView(StatusState.IDLE)

