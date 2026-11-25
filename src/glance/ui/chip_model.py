"""Pure chip state model: inputs in, what the chip should show out."""
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


# The old chip lives on only as a small status dot, for the states that suppress the strip.
DOT_STATES = frozenset({ChipState.PAUSED, ChipState.PRIVATE, ChipState.AI_LIMIT,
                        ChipState.NO_AI})


def shows_dot(view: ChipView) -> bool:
    """True when the chip state is drawn (as the 8 px dot); every other state is the strip's
    business, or nothing at all."""
    return view.state in DOT_STATES
