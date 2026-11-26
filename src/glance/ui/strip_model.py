"""Pure strip model: the corrected text shown under the field. No GTK.

The strip shows the field's whole text (every sentence and paragraph) with its corrections
applied (spelling, grammar, punctuation, capitalisation: `grammar.corrections`), in the user's
own words and tone: never a rewording. Nothing is asked of an AI for it: it is built from
the issues the controller already has. Protected entities (mentions, links, emoji, embedded
objects) are never retyped: fixes on either side of one are never merged across it.

A corrected text longer than about four lines is shown compact: only its changed sentences,
each in its own context, separated by "…", with a line saying how many fixes there are. The
strip's "⋯" expands it to the whole corrected text (the renderers scroll it).
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from glance.grammar.corrections import strip_replacement
from glance.grammar.issues import Issue, merge_edits
from glance.privacy.entities import Span, find_protected, overlaps
from glance.protocol import Edit, Rect

COMPACT_CHARS = 320  # a corrected text longer than this (about four lines) is shown compact
COMPACT_LINES = 4
EXCERPT_CHARS = 160  # a changed sentence longer than this is cut to its changes
CONTEXT_CHARS = 40   # ... and this much on either side of them
ELLIPSIS = "…"
SCROLL_SHARE = 0.4  # the expanded text scrolls past this share of the monitor's height
_SENTENCE_END = re.compile(r"[.!?…]+[\"')\]]*\s+")
_TOKEN = re.compile(r"\w+(?:['’]\w+)*|\s+|[^\w\s]")


@dataclass(frozen=True)
class Segment:
    text: str
    changed: bool  # a fix: drawn in the accent colour, bold


@dataclass(frozen=True)
class StripView:
    """What the collapsed strip shows. Empty (no segments, note or guard) means hidden."""

    # The corrected text, or its compact view when `full` is set; empty: nothing to use.
    segments: tuple[Segment, ...] = ()
    n_fixes: int = 0
    note: str | None = None  # e.g. "Reads blunt for team channel — ⋯ for tone options"
    guard: str | None = None  # the send-guard's question, e.g. "Sounds hostile"
    expanded: bool = False  # the drawer is open under it
    menu: bool = True  # offers "⋯" (not for a paused or private field: there is no drawer)
    on_demand: bool = False  # opened by the user (status icon, panel hotkey): Esc closes it
    full: tuple[Segment, ...] = ()  # compact: the whole corrected text, shown when expanded
    summary: str | None = None  # compact: "7 fixes across the message"
    meta: str | None = None  # muted, e.g. "Long text: AI checked 600 of 1,450 words …"

    @property
    def compact(self) -> bool:
        return bool(self.full)

    @property
    def shown(self) -> tuple[Segment, ...]:
        """What the renderers draw: the whole text once "⋯" expanded a compact view (in a
        scrollable area), else `segments`."""
        return self.full if self.expanded and self.full else self.segments

    @property
    def can_use(self) -> bool:
        return bool(self.segments) and self.n_fixes > 0

    @property
    def empty(self) -> bool:
        return not self.segments and self.note is None and self.guard is None

    @property
    def text(self) -> str:
        """The whole corrected text (what "Use" types), compact or not."""
        return "".join(s.text for s in (self.full or self.segments))


@dataclass(frozen=True)
class StripFix:
    """The corrected unit and the one edit that makes it so."""

    segments: tuple[Segment, ...]
    edit: Edit
    n_fixes: int


def text_unit(text: str) -> tuple[int, int]:
    """[start, end) of the whole text without its leading and trailing whitespace."""
    start, end = 0, len(text)
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


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
    """The whole text with its fixes applied, or None when it has nothing to fix.

    Uses the corrections only (`corrections.strip_replacement`: safe and confident Harper
    fixes, AI-analysis issues of a correction kind), never a rewording, in position order; one that overlaps an earlier one waits. A fix touching a
    protected entity is left out, and when entities split the fixes into runs only the run
    nearest the caret is used: the edit never spans an entity. Without any entity in the
    text the edit replaces the whole field (one undo step for the whole message).

    What is highlighted, and counted, comes from a word-level diff of the unit against its
    corrected text, not from the issue spans: every changed word is shown changed, and a
    change is one run of changed words within a fix (one fix that splits "likethe" is one
    change; one that fixes two words apart is two).
    """
    u0, u1 = text_unit(text)
    if u1 <= u0:
        return None
    protected = find_protected(text)
    edits: list[Edit] = []
    for i in sorted(issues, key=lambda i: (i.start, i.end)):
        if not u0 <= i.start <= i.end <= u1:
            continue
        r = strip_replacement(text, i)
        if r is None:
            continue
        e = Edit(i.start, i.end, r)
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
    if not protected:
        merged = Edit(0, len(text), text[:merged.start] + merged.replacement + text[merged.end:])
    segments, n = _segments(text, u0, u1, chosen)
    if n == 0:
        return None
    return StripFix(segments, merged, n)


def _word_char(ch: str) -> bool:
    return ch.isalnum() or ch in "_'’"


def _chunks(text: str, u0: int, u1: int, edits: list[Edit]) -> list[tuple[int, int, str]]:
    """Each edit widened to whole words, as (start, end, corrected text); edits whose widened
    spans touch are one chunk (two fixes inside one word)."""
    out: list[tuple[int, int, list[Edit]]] = []
    for e in edits:
        x0, x1 = e.start, e.end
        while x0 > u0 and _word_char(text[x0 - 1]):
            x0 -= 1
        while x1 < u1 and _word_char(text[x1]):
            x1 += 1
        if out and x0 < out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], x1), [*out[-1][2], e])
        else:
            out.append((x0, x1, [e]))
    chunks = []
    for x0, x1, es in out:
        parts, pos = [], x0
        for e in es:
            parts += [text[pos:e.start], e.replacement]
            pos = e.end
        parts.append(text[pos:x1])
        chunks.append((x0, x1, "".join(parts)))
    return chunks


def _changes(before: str, after: str) -> list[tuple[int, int]]:
    """[start, end) in `after` of each change from `before`, by a word-level diff: runs of
    changed words, runs only whitespace keeps apart being one change (empty: a deletion)."""
    a, b = _TOKEN.findall(before), _TOKEN.findall(after)
    offs = [0]
    for t in b:
        offs.append(offs[-1] + len(t))
    regions: list[tuple[int, int]] = []
    for op, _i1, _i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        start, end = offs[j1], offs[j2]
        if regions and not after[regions[-1][1]:start].strip():
            regions[-1] = (regions[-1][0], end)
        else:
            regions.append((start, end))
    return regions


def _segments(text: str, u0: int, u1: int, edits: list[Edit]) -> tuple[tuple[Segment, ...], int]:
    """The corrected unit as segments, every changed word marked, and the number of changes."""
    segments: list[Segment] = []
    n = 0
    pos = u0
    for x0, x1, new in _chunks(text, u0, u1, edits):
        if x0 > pos:
            segments.append(Segment(text[pos:x0], False))
        at = 0
        for c0, c1 in _changes(text[x0:x1], new):
            n += 1
            if c0 > at:
                segments.append(Segment(new[at:c0], False))
            if c1 > c0:
                segments.append(Segment(new[c0:c1], True))
            at = max(at, c1)
        if at < len(new):
            segments.append(Segment(new[at:], False))
        pos = x1
    if pos < u1:
        segments.append(Segment(text[pos:u1], False))
    return _joined(segments), n


def _joined(segments: list[Segment]) -> tuple[Segment, ...]:
    out: list[Segment] = []
    for seg in segments:
        if out and out[-1].changed == seg.changed:
            out[-1] = Segment(out[-1].text + seg.text, seg.changed)
        else:
            out.append(seg)
    return tuple(out)


def _distance(group: list[Edit], caret: int) -> int:
    lo, hi = group[0].start, group[-1].end
    return 0 if lo <= caret <= hi else min(abs(caret - lo), abs(caret - hi))


def strip_view(fix: StripFix | None) -> StripView:
    if fix is None:
        return StripView()
    text = "".join(seg.text for seg in fix.segments)
    if len(text) <= COMPACT_CHARS and text.count("\n") < COMPACT_LINES:
        return StripView(fix.segments, fix.n_fixes)
    return StripView(compact_segments(fix.segments), fix.n_fixes, full=fix.segments,
                     summary=fixes_summary(fix.n_fixes))


def fixes_summary(n: int) -> str:
    return f"{n} fix{'' if n == 1 else 'es'} across the message"


def _sentences(text: str) -> list[tuple[int, int]]:
    """[start, end) of each sentence (a line break ends one too), whitespace left out."""
    out, pos = [], 0
    for m in re.finditer(r"[.!?…]+[\"')\]]*(?=\s)|\n", text):
        out.append((pos, m.end()))
        pos = m.end()
    out.append((pos, len(text)))
    spans = []
    for a, b in out:
        while a < b and text[a].isspace():
            a += 1
        while b > a and text[b - 1].isspace():
            b -= 1
        if b > a:
            spans.append((a, b))
    return spans


def _slice(segments: tuple[Segment, ...], a: int, b: int) -> list[Segment]:
    out, pos = [], 0
    for seg in segments:
        s0, s1 = pos, pos + len(seg.text)
        pos = s1
        lo, hi = max(a, s0), min(b, s1)
        if lo < hi:
            out.append(Segment(seg.text[lo - s0:hi - s0], seg.changed))
    return out


def compact_segments(segments: tuple[Segment, ...]) -> tuple[Segment, ...]:
    """Only the changed sentences of the corrected text, each with a little context (a long
    one cut to its changes), separated by "…"; at most about COMPACT_CHARS long."""
    text = "".join(seg.text for seg in segments)
    changes, pos = [], 0
    for seg in segments:
        if seg.changed:
            changes.append((pos, pos + len(seg.text)))
        pos += len(seg.text)
    excerpts: list[tuple[int, int]] = []
    for a, b in _sentences(text):
        inside = [(c0, c1) for c0, c1 in changes if c0 < b and a < c1]
        if not inside:
            continue
        if b - a > EXCERPT_CHARS:
            a2 = max(a, inside[0][0] - CONTEXT_CHARS)
            b2 = min(b, inside[-1][1] + CONTEXT_CHARS)
            while a2 > a and not text[a2 - 1].isspace():  # whole words only
                a2 -= 1
            while b2 < b and not text[b2].isspace():
                b2 += 1
            a, b = a2, b2
        if excerpts and a <= excerpts[-1][1]:
            excerpts[-1] = (excerpts[-1][0], max(b, excerpts[-1][1]))
        else:
            excerpts.append((a, b))
    out: list[Segment] = []
    length = 0
    for k, (a, b) in enumerate(excerpts):
        if k and length + (b - a) > COMPACT_CHARS:
            out.append(Segment(f" {ELLIPSIS}", False))
            break
        gap = text[excerpts[k - 1][1]:a] if k else text[:a]
        if gap.strip():
            out.append(Segment(f"{ELLIPSIS} " if not k else f" {ELLIPSIS} ", False))
        elif k:
            out.append(Segment(" ", False))
        out += _slice(segments, a, b)
        length += b - a
    else:
        if excerpts and text[excerpts[-1][1]:].strip():
            out.append(Segment(f" {ELLIPSIS}", False))
    return _joined(out)


def scroll_max_h(monitors: list[Rect], x: int | None = None, y: int | None = None) -> int:
    """The tallest the expanded corrected text may be before it scrolls: SCROLL_SHARE of the
    height of the monitor holding (x, y) (the first one otherwise)."""
    mon = next((m for m in monitors if x is not None and y is not None
                and m.x <= x < m.x + m.w and m.y <= y < m.y + m.h),
               monitors[0] if monitors else None)
    return max(120, int((mon.h if mon is not None else 1080) * SCROLL_SHARE))
