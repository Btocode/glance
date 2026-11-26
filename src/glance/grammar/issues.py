"""Issue model and edit helpers. All offsets are code points into the field text."""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise
from typing import NamedTuple

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


SPELL_RULE = "SpellCheck"
_RULE_KEY = re.compile(r"^[A-Z][A-Za-z0-9]*$")


class HarperLint(NamedTuple):
    """One Harper lint before it becomes an Issue, from either engine (harper-ls or the
    in-process `glance_core`). Offsets are code points; `code` is Harper's rule key."""

    start: int
    end: int
    code: object
    message: str
    replacements: tuple[str, ...]


def harper_issues(lints: Iterable[HarperLint]) -> list[Issue]:
    """The one place a Harper lint becomes an Issue, so both engines behave the same."""
    out: list[Issue] = []
    seen: set[tuple[int, int, str]] = set()
    for lint in lints:
        start, end, code, repl = lint.start, lint.end, lint.code, lint.replacements
        # Some spelling lints carry the replacement word as their rule key ("The" for
        # "Teh"): not a rule then.
        rule = code if isinstance(code, str) and _RULE_KEY.match(code) and code not in repl \
            else SPELL_RULE
        key = (start, end, str(code))
        if key in seen:
            continue
        seen.add(key)
        out.append(Issue(f"harper:{rule}:{start}:{end}", "harper", rule, lint.message,
                         start, end, repl, len(repl) == 1 and rule != SPELL_RULE))
    return out


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
