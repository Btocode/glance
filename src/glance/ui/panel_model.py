"""Pure panel model: what the panel shows for a field's text and issues."""
from __future__ import annotations

from dataclasses import dataclass

from glance.diffing import DiffOp
from glance.grammar.issues import Issue

EXCERPT_CONTEXT = 20


@dataclass(frozen=True)
class IssueRow:
    issue_id: str
    excerpt: str  # the flagged text with up to EXCERPT_CONTEXT characters either side
    message: str
    replacements: tuple[str, ...]
    rule: str
    flagged: str = ""  # exactly the flagged text, for the drawer's one-line fix list


@dataclass(frozen=True)
class ToneChip:
    key: str
    label: str
    hotkey: int | None
    # The rewrite in this tone as far as it is known (a card's one-line preview), and
    # loading | ready | failed while there is one.
    preview: str | None = None
    state: str | None = None


@dataclass(frozen=True)
class PanelView:
    tone: str | None = None
    tone_reason: str | None = None
    tone_chips: tuple[ToneChip, ...] = ()
    issues: tuple[IssueRow, ...] = ()
    banner: str | None = None
    can_fix_all: bool = False
    copy_offer: str | None = None
    # A tone rewrite of the whole text: the diff so far, loading | ready | failed, and
    # whether it may be typed into the field (if not, `copy_offer` holds it).
    preview: tuple[DiffOp, ...] | None = None
    preview_state: str | None = None
    can_apply: bool = False
    profile_label: str | None = None
    profiles: tuple[tuple[str, str], ...] = ()  # (name, label) the audience can be set to
    # `field`: the focused field's panel. `selection`: select-anything, a read-only issue list,
    # a rewrite to copy and, where it is safe, a paste over the selection.
    mode: str = "field"
    can_paste: bool = False
    # A question that must be answered before anything leaves the machine.
    confirm: str | None = None
    # The send-guard's warning for the text in the field (e.g. "Sounds hostile").
    guard: str | None = None


def _excerpt(text: str, start: int, end: int) -> str:
    return text[max(0, start - EXCERPT_CONTEXT):end + EXCERPT_CONTEXT]


def build_panel(text: str, issues: list[Issue], banner: str | None = None,
                copy_offer: str | None = None) -> PanelView:
    """The grammar part of the panel; the tone fields are filled in by `extra_panel` hooks."""
    rows = tuple(IssueRow(i.id, _excerpt(text, i.start, i.end), i.message, i.replacements,
                          i.rule, text[i.start:i.end]) for i in issues)
    return PanelView(issues=rows, banner=banner, can_fix_all=any(i.safe for i in issues),
                     copy_offer=copy_offer)
