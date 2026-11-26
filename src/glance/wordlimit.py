"""How much of a long text Glance analyses at once. Pure.

Words are counted with a plain whitespace split (any script: Banglish, Bangla, emoji). When a
text has more words than a limit, only a window of that many words around the caret is used,
its edges moved in to a paragraph or sentence boundary where there is one.
"""
from __future__ import annotations

import re

DEFAULT_MAX_WORDS = 600  # `[ai] max_words`
MIN_MAX_WORDS = 50
MAX_MAX_WORDS = 5000
LOCAL_MAX_WORDS = 5000  # the local checker's hard cap, whatever `max_words` says

_WORD = re.compile(r"\S+")
# Where a sentence or paragraph ends: the position right after it.
_BOUNDARY = re.compile(r"[.!?…]+[\"')\]]*\s+|\n\s*")


def count_words(text: str) -> int:
    return len(text.split())


def word_window(text: str, caret: int, max_words: int) -> tuple[int, int]:
    """[start, end) of the part of `text` to analyse: all of it when it has at most
    `max_words` words, else at most `max_words` words around the caret, aligned to sentence
    or paragraph boundaries (word boundaries when one sentence is that long)."""
    words = [m.span() for m in _WORD.finditer(text)]
    n = len(words)
    if n <= max_words or max_words <= 0:
        return 0, len(text)
    caret = max(0, min(caret, len(text)))
    at = next((i for i, (_s, e) in enumerate(words) if e >= caret), n - 1)
    lo = max(0, at - max_words // 2)
    hi = min(n, lo + max_words)
    lo = max(0, hi - max_words)
    start, end = words[lo][0], words[hi - 1][1]
    if lo > 0 and not _at_boundary(text, start):
        # In from the start: right after the first boundary inside, if the caret stays in.
        m = _BOUNDARY.search(text, start, end)
        if m is not None and m.end() <= caret:
            start = m.end()
    if hi < n:
        # In from the end: right after the last sentence that ends inside it.
        last = None
        for m in _BOUNDARY.finditer(text, start, min(len(text), end + 1)):
            last = m.start() + len(m.group().rstrip())
        if last is not None and last > start and last >= caret:
            end = last
    while start < end and text[start].isspace():
        start += 1
    return start, end


def _at_boundary(text: str, pos: int) -> bool:
    before = text[:pos].rstrip(" \t")
    return not before or before.endswith("\n") or (
        pos > len(before) and bool(re.search(r"[.!?…]+[\"')\]]*\Z", before)))


def limit_notice(checked: int, total: int, *, verb: str = "checked",
                 where: str = "around your cursor") -> str:
    """The muted line that says only part of a long text went to the AI."""
    return f"Long text: AI {verb} {checked:,} of {total:,} words {where}"
