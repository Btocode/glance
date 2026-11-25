"""Protected entities: spans of a field's text that no edit may touch."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

OBJECT = "￼"  # AT-SPI embedded-object placeholder (Slack emoji, links, code: S2.5)


@dataclass(frozen=True)
class Span:
    """Half-open code-point range `[start, end)` of kind object|url|mention|emoji|code."""

    start: int
    end: int
    kind: str


_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # A fence that is never closed protects everything after it.
    ("code", re.compile(r"```.*?(?:```|\Z)|`[^`\n]+`", re.DOTALL)),
    ("url", re.compile(r"https?://\S+|www\.\S+")),
    ("mention", re.compile(r"(?<!\w)@[\w.-]+|<@[^>\n]*>|(?<!\w)#[\w-]+")),
    ("emoji", re.compile(r":[a-z0-9_+-]+:")),
)

# Characters that extend an emoji but are not one alone: ZWJ, VS16, keycap, tag characters.
_ZWJ = "‍"
_EMOJI_JOINERS = frozenset({_ZWJ, "️", "⃣"} | {chr(c) for c in range(0xE0020, 0xE0080)})


def _is_emoji(ch: str) -> bool:
    if ch == OBJECT:
        return False
    return 0x1F000 <= ord(ch) <= 0x1FAFF or unicodedata.category(ch) == "So"


def _emoji_runs(text: str) -> list[Span]:
    """One span per run of emoji characters, so ZWJ sequences, flags and modifiers stay whole."""
    spans = []
    i, n = 0, len(text)
    while i < n:
        if not _is_emoji(text[i]):
            i += 1
            continue
        j = i + 1
        while j < n and (_is_emoji(text[j]) or text[j] in _EMOJI_JOINERS):
            j += 1
        while text[j - 1] == _ZWJ:  # a dangling joiner belongs to what follows
            j -= 1
        spans.append(Span(i, j, "emoji"))
        i = j
    return spans


def find_protected(text: str) -> list[Span]:
    """Protected spans, sorted and non-overlapping.

    Overlapping matches merge into one span that keeps the kind of the match starting first
    (the longer one on a tie).
    """
    found = [Span(i, i + 1, "object") for i, ch in enumerate(text) if ch == OBJECT]
    for kind, rx in _PATTERNS:
        found += [Span(m.start(), m.end(), kind) for m in rx.finditer(text)]
    found += _emoji_runs(text)
    found.sort(key=lambda s: (s.start, -s.end))
    merged: list[Span] = []
    for s in found:
        if merged and s.start < merged[-1].end:
            last = merged[-1]
            merged[-1] = Span(last.start, max(last.end, s.end), last.kind)
        else:
            merged.append(s)
    return merged


def overlaps(spans: list[Span], start: int, end: int) -> bool:
    """True if the edit range `[start, end)` touches any span.

    An insertion (`start == end`) overlaps only when it falls strictly inside a span.
    """
    return any(s.start < end and start < s.end for s in spans)
