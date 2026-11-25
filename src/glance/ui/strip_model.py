"""Pure strip model: the one corrected sentence shown under the field. No GTK.

The strip shows the sentence (or short paragraph) around the caret with every safe local fix
and every AI-analysis replacement applied. Nothing is asked of an AI for it: it is built from
the issues the controller already has. Protected entities (mentions, links, emoji, embedded
objects) are never retyped: fixes on either side of one are never merged across it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from glance.grammar.issues import Issue, issue_edit, merge_edits
from glance.privacy.entities import Span, find_protected, overlaps
from glance.protocol import Edit

MAX_UNIT_CHARS = 280  # a longer paragraph is cut down to its sentence around the caret
_SENTENCE_END = re.compile(r"[.!?…]+[\"')\]]*\s+")


@dataclass(frozen=True)
class Segment:
    text: str
    changed: bool  # a fix: drawn in the accent colour, bold


@dataclass(frozen=True)
class StripView:
    """What the collapsed strip shows. Empty (no segments, note or guard) means hidden."""

    segments: tuple[Segment, ...] = ()  # the corrected sentence; empty: nothing to use
    n_fixes: int = 0
    note: str | None = None  # e.g. "Reads blunt for team channel — ⋯ for tone options"
    guard: str | None = None  # the send-guard's question, e.g. "Sounds hostile"
    expanded: bool = False  # the drawer is open under it

    @property
    def can_use(self) -> bool:
        return bool(self.segments) and self.n_fixes > 0

    @property
    def empty(self) -> bool:
        return not self.segments and self.note is None and self.guard is None

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.segments)


@dataclass(frozen=True)
class StripFix:
    """The corrected unit and the one edit that makes it so."""

    segments: tuple[Segment, ...]
    edit: Edit
    n_fixes: int


def unit_around(text: str, caret: int) -> tuple[int, int]:
    """[start, end) of the paragraph holding the caret, or of its sentence when the paragraph
    is longer than MAX_UNIT_CHARS. Leading and trailing whitespace is left out."""
    caret = max(0, min(caret, len(text)))
    start = text.rfind("\n", 0, caret) + 1
    end = text.find("\n", caret)
    end = len(text) if end < 0 else end
    if end - start > MAX_UNIT_CHARS:
        s0, s1 = start, end
        for m in _SENTENCE_END.finditer(text, start, end):
            if m.end() <= caret:
                s0 = m.end()
            else:
                s1 = m.end()
                break
        start, end = s0, s1
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _usable(i: Issue) -> bool:
    if not i.replacements:
        return False
    return i.safe if i.source == "harper" else i.source == "llm"


def _groups(edits: list[Edit], protected: list[Span]) -> list[list[Edit]]:
    """Runs of consecutive edits with no protected span between them."""
    groups: list[list[Edit]] = []
    for e in edits:
        if groups and not overlaps(protected, groups[-1][-1].end, e.start):
            groups[-1].append(e)
        else:
            groups.append([e])
    return groups


def strip_fix(text: str, caret: int, issues: list[Issue] | tuple[Issue, ...]) -> StripFix | None:
    """The unit around the caret with its fixes applied, or None when it has nothing to fix.

    Uses the safe Harper fixes and the AI-analysis replacements (first choice) that lie whole
    inside the unit, in position order; one that overlaps an earlier one waits. A fix touching
    a protected entity is left out, and when entities split the fixes into runs only the run
    nearest the caret is used: the edit never spans an entity.
    """
    u0, u1 = unit_around(text, caret)
    if u1 <= u0:
        return None
    protected = find_protected(text)
    edits: list[Edit] = []
    for i in sorted(issues, key=lambda i: (i.start, i.end)):
        if not (_usable(i) and u0 <= i.start <= i.end <= u1):
            continue
        e = issue_edit(i)
        if e.replacement == text[e.start:e.end] or overlaps(protected, e.start, e.end):
            continue
        if edits and e.start < edits[-1].end:
            continue
        edits.append(e)
    if not edits:
        return None
    groups = _groups(edits, protected)
    chosen = min(groups, key=lambda g: (_distance(g, caret), g[0].start))
    merged = merge_edits(text, chosen)
    segments: list[Segment] = []
    pos = u0
    for e in chosen:
        if e.start > pos:
            segments.append(Segment(text[pos:e.start], False))
        if e.replacement:
            segments.append(Segment(e.replacement, True))
        pos = e.end
    if pos < u1:
        segments.append(Segment(text[pos:u1], False))
    return StripFix(tuple(segments), merged, len(chosen))


def _distance(group: list[Edit], caret: int) -> int:
    lo, hi = group[0].start, group[-1].end
    return 0 if lo <= caret <= hi else min(abs(caret - lo), abs(caret - hi))


def strip_view(fix: StripFix | None) -> StripView:
    return StripView() if fix is None else StripView(fix.segments, fix.n_fixes)
