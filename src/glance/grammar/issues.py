"""Issue model and edit helpers. All offsets are code points into the field text."""
from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

from glance.protocol import Edit


@dataclass(frozen=True)
class Issue:
    """One suggestion. `id` is `source:rule:start:end`; `safe` means one-click fixable."""

    id: str
    source: str  # "harper" | "llm"
    rule: str
    message: str
    start: int
    end: int
    replacements: tuple[str, ...]
    safe: bool


def utf16_to_cp(text: str, line: int, character: int) -> int:
    """Map an LSP position (line, UTF-16 column) to a code-point offset into `text`."""
    offset = 0
    lines = text.split("\n")
    if line >= len(lines):
        return len(text)
    for ln in lines[:line]:
        offset += len(ln) + 1
    units = 0
    for i, ch in enumerate(lines[line]):
        if units >= character:
            return offset + i
        units += 2 if ord(ch) > 0xFFFF else 1
    return offset + len(lines[line])


def issue_edit(issue: Issue, choice: int = 0) -> Edit:
    return Edit(issue.start, issue.end, issue.replacements[choice])


def apply_edit(text: str, edit: Edit) -> str:
    return text[: edit.start] + edit.replacement + text[edit.end :]


def merge_edits(text: str, edits: list[Edit]) -> Edit:
    """The single contiguous Edit equal to applying all `edits`; ValueError on overlap."""
    if not edits:
        raise ValueError("no edits")
    ordered = sorted(edits, key=lambda e: (e.start, e.end))
    for a, b in pairwise(ordered):
        if b.start < a.end:
            raise ValueError("overlapping edits")
    lo, hi = ordered[0].start, max(e.end for e in ordered)
    parts: list[str] = []
    pos = lo
    for e in ordered:
        parts.append(text[pos : e.start])
        parts.append(e.replacement)
        pos = e.end
    return Edit(lo, hi, "".join(parts))
