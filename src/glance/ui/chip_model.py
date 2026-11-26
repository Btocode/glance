"""Pure chip state model: inputs in, the field's state out.

Nothing draws the chip any more (the status icon, `status_model`, took its place); its state
still feeds the tray, the strip's gating and the test hooks."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ChipState(Enum):
    HIDDEN = "hidden"
    IDLE = "idle"
    FIXES = "fixes"
    SPINNER = "spinner"
    TONE = "tone"
    PAUSED = "paused"
    PRIVATE = "private"
    AI_LIMIT = "ai_limit"
    NO_AI = "no_ai"
    GUARD = "guard"


@dataclass(frozen=True)
class ChipInputs:
    has_field: bool = False
    paused: bool = False
    gated: str | None = None
    text_len: int = 0
    settled: bool = False
    snoozed: bool = False
    n_local: int = 0
    analyzing: bool = False
    tone_label: str | None = None
    n_llm: int = 0
    mismatch: bool = False
    ai: str = "off"  # off | ok | none | limit
    guard: str | None = None
    ai_pending: bool = False  # an AI analysis for this text is asked for and not back yet
    ai_checking: bool = False  # ...and the user asked for it (check shortcut, icon click)


@dataclass(frozen=True)
class ChipView:
    state: ChipState
    label: str
    badge: str | None


def chip_view(i: ChipInputs, min_chars: int = 15) -> ChipView:
    if not i.has_field:
        return ChipView(ChipState.HIDDEN, "", None)
    if i.paused:
        return ChipView(ChipState.PAUSED, "Paused", None)
    if i.gated:
        return ChipView(ChipState.PRIVATE, "Private", None)
    if not i.settled or i.text_len < min_chars or i.snoozed:
        return ChipView(ChipState.HIDDEN, "", None)
    n = i.n_local + i.n_llm
    if i.guard:
        return ChipView(ChipState.GUARD, i.guard, None)
    if i.mismatch:
        label = i.tone_label or ""
        return ChipView(ChipState.TONE, f"{label} · {n}" if n else label, None)
    if i.analyzing:
        return ChipView(ChipState.SPINNER, "", None)
    if n > 0:
        badge = i.ai if i.ai in ("limit", "none") else None
        return ChipView(ChipState.FIXES, str(n), badge)
    if i.ai == "limit":
        return ChipView(ChipState.AI_LIMIT, "AI paused (limit)", None)
    if i.ai == "none":
        return ChipView(ChipState.NO_AI, "Connect an AI", None)
    return ChipView(ChipState.IDLE, "", None)

