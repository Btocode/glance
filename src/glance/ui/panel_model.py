"""Pure panel model: what the panel shows for a field's text and issues."""
from __future__ import annotations

from dataclasses import dataclass

from glance.diffing import DiffOp
from glance.grammar.issues import Issue
from glance.ui.strip_model import Segment, StripView

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
    # The user clicked it (or pressed its number key): its rewrite is the one on show in
    # "Suggested", and the one Apply types. A second click goes back to the corrected text.
    selected: bool = False


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
    preview_label: str | None = None  # the selected tone's name ("Friendlier")
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


@dataclass(frozen=True)
class SuggestedBox:
    """The panel's "Suggested" box, the same in every renderer: the corrected text, or the
    rewrite in the tone the user selected (`tone`: its name beside "Suggested")."""

    segments: tuple[Segment, ...]  # changed words are tinted
    tone: str | None = None
    working: bool = False  # the selected tone's rewrite is still being written
    placeholder: str | None = None  # muted, in the box, while there is no text to show
    lines: tuple[str, ...] = ()  # muted lines under it (the strip's summary and meta)
    can_apply: bool = False  # "Apply (Alt+Enter)" (the `use` action) types it
    scroll: bool = False  # long: it scrolls


REWRITING = "Rewriting…"
SCROLL_CHARS = 320  # a rewrite longer than this scrolls in "Suggested"
NO_REWRITE = "No rewrite this time."


def preview_segments(ops: tuple[DiffOp, ...]) -> tuple[Segment, ...]:
    """A rewrite's diff as the text it makes, the words it put in marked changed."""
    return tuple(Segment(op.text, op.kind == "ins") for op in ops if op.kind != "del")


def suggested_box(view: PanelView, strip: StripView | None) -> SuggestedBox | None:
    """What "Suggested" shows in the field's panel: the selected tone's rewrite (streaming in,
    then ready to apply), else the strip's corrected text; None for neither."""
    if view.mode == "field" and view.preview_state is not None:
        segs = preview_segments(view.preview or ())
        working = view.preview_state == "loading"
        placeholder = None
        if not segs:
            placeholder = REWRITING if working else NO_REWRITE
        long = sum(len(x.text) for x in segs) > SCROLL_CHARS
        return SuggestedBox(segs, view.preview_label or "Rewrite", working, placeholder,
                            can_apply=view.can_apply, scroll=long)
    if strip is not None and strip.segments:
        return SuggestedBox(strip.shown, lines=tuple(x for x in (strip.summary, strip.meta) if x),
                            can_apply=strip.can_use, scroll=strip.compact)
    return None


def _excerpt(text: str, start: int, end: int) -> str:
    return text[max(0, start - EXCERPT_CONTEXT):end + EXCERPT_CONTEXT]


def build_panel(text: str, issues: list[Issue], banner: str | None = None,
                copy_offer: str | None = None) -> PanelView:
    """The grammar part of the panel; the tone fields are filled in by `extra_panel` hooks."""
    rows = tuple(IssueRow(i.id, _excerpt(text, i.start, i.end), i.message, i.replacements,
                          i.rule, text[i.start:i.end]) for i in issues)
    return PanelView(issues=rows, banner=banner, can_fix_all=any(i.safe for i in issues),
                     copy_offer=copy_offer)
