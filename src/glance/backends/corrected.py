"""The AI's corrected text as issues: a diff of the draft against it, minimal edits only.

The model answers an analysis with the whole draft corrected (`Analysis.corrected`) rather
than with spans to fix. Every change it made becomes one issue, anchored by offsets the diff
computes (not by quotes the model had to copy), widened to the whole words around it so the
drawer's list reads "doing , -> doing," rather than " -> (remove)". A change is kept only when
it is a correction:

- a minimal edit (`corrections.minimal_edit`: a typo, an inflection, a split or join, case,
  punctuation, spacing, at most two words added or swapped), never a rewording;
- clear of protected content (a placeholder `[[GLANCE_...]]` or an entity);
- and the answer as a whole is a correction of the draft, not a rewrite of it (most of its
  words are the draft's): otherwise nothing is kept.

Pure; offsets are code points into the text the model was given.
"""
from __future__ import annotations

import difflib
import re

from glance.grammar.corrections import minimal_edit, near_miss
from glance.grammar.issues import Issue
from glance.privacy.entities import find_protected

_TOKEN = re.compile(r"\w+(?:['’]\w+)*|\s+|[^\w\s]")
_PLACEHOLDER = re.compile(r"\[\[GLANCE_[A-Z]\d+\]\]")
MIN_SAME = 0.5  # at least this share of the draft's words must survive: else a rewrite
MESSAGES = {
    "capitalization": "Capitalization",
    "punctuation": "Punctuation and spacing",
    "spelling": "Spelling",
    "missing_word": "Missing word",
    "grammar": "Grammar",
}


def _kind(old: str, new: str) -> str:
    if old.lower() == new.lower():
        return "capitalization"
    if re.sub(r"[\W_]+", "", old).lower() == re.sub(r"[\W_]+", "", new).lower():
        return "punctuation"
    a, b = re.findall(r"\w+", old), re.findall(r"\w+", new)
    if len(b) > len(a):
        return "missing_word"
    if len(a) == len(b) == 1:
        return "spelling"
    return "grammar"


def _norm(token: str) -> str:
    return " " if token.isspace() and "\n" not in token else token.lower()


def _hunks(text: str, corrected: str) -> tuple[list[tuple[int, int, str]], float]:
    """Each change as (start, end, replacement) in `text`, and the share of words kept."""
    a, b = _TOKEN.findall(text), _TOKEN.findall(corrected)
    pa, pb = [0], [0]
    for t in a:
        pa.append(pa[-1] + len(t))
    for t in b:
        pb.append(pb[-1] + len(t))
    # Aligned ignoring case and the width of spaces ("hey" is "Hey" fixed, not a new word);
    # those differences are changes all the same, found token by token in the equal runs.
    sm = difflib.SequenceMatcher(str.isspace, [_norm(t) for t in a], [_norm(t) for t in b],
                                 autojunk=False)
    out = []
    same = 0
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            same += sum(1 for t in a[i1:i2] if not t.isspace())
            for k in range(i2 - i1):
                if a[i1 + k] != b[j1 + k]:
                    out.append((pa[i1 + k], pa[i1 + k + 1], b[j1 + k]))
            continue
        if op == "replace" and i2 - i1 == j2 - j1:  # word for word: each pair its own
            for k in range(i2 - i1):
                if a[i1 + k] != b[j1 + k]:
                    out.append((pa[i1 + k], pa[i1 + k + 1], b[j1 + k]))
            continue
        out.append((pa[i1], pa[i2], corrected[pb[j1]:pb[j2]]))
    words = sum(1 for t in a if not t.isspace())
    return out, (same / words if words else 1.0)


def _no_swap(old: str, new: str) -> bool:
    """No word swapped for a different one ("plan" -> "strategy"), but for near misses
    ("seem" -> "seems", "teh" -> "the") and short function words ("is" -> "are")."""
    a = [w.lower() for w in re.findall(r"\w+(?:['’]\w+)*", old)]
    b = [w.lower() for w in re.findall(r"\w+(?:['’]\w+)*", new)]
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != "replace" or "".join(a[i1:i2]) == "".join(b[j1:j2]):
            continue
        for w in a[i1:i2]:
            if not any(near_miss(w, n) or (len(w) <= 4 and len(n) <= 4) for n in b[j1:j2]):
                return False
    return True


def _widen(text: str, start: int, end: int) -> tuple[int, int]:
    """To the whitespace on either side: whole words, with the punctuation stuck to them."""
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    while end < len(text) and not text[end].isspace():
        end += 1
    return start, end


_PIECE = re.compile(r"\w+(?:['’]\w+)*|[^\w\s]")


def _pieces(body: str, merged: list[tuple[int, int, list[tuple[int, int, str]]]]
            ) -> list[tuple[int, int, str]]:
    """Each merged change as (start, end, corrected words). One that is not a minimal edit
    as a whole ("seem sal alright ," -> "seems all right,") is split word for word when
    both sides have as many words, so its good parts ("seem" -> "seems") still count."""
    out = []
    for x0, x1, hs in merged:
        parts, pos = [], x0
        for h0, h1, rep in hs:
            parts += [body[pos:h0], rep]
            pos = h1
        parts.append(body[pos:x1])
        old, new = body[x0:x1], "".join(parts)
        if minimal_edit(old, new) and _no_swap(old, new):
            out.append((x0, x1, new))
            continue
        a = [(m.start() + x0, m.end() + x0) for m in _PIECE.finditer(old)]
        b = list(_PIECE.finditer(new))
        if len(a) != len(b) or len(a) < 2:
            out.append((x0, x1, new))  # judged (and dropped) whole
            continue
        for (s0, s1), m in zip(a, b):
            w = m.group()
            if not w[0].isalnum() and m.start() and not new[m.start() - 1].isspace():
                while s0 > x0 and body[s0 - 1].isspace():  # "doing ," -> "doing?"
                    s0 -= 1
            if body[s0:s1] != w:
                out.append((s0, s1, w))
    return out


def corrected_issues(text: str, corrected: str) -> list[Issue] | None:
    """The corrections the model made to `text`, as issues of source "llm"; None when its
    answer is a rewrite rather than a correction (then it corrects nothing)."""
    lead = len(text) - len(text.lstrip())
    trail = len(text.rstrip())
    body = text[lead:trail]
    corrected = corrected.strip()
    if not body or corrected == body:
        return []
    hunks, kept = _hunks(body, corrected)
    if kept < MIN_SAME:
        return None  # a rewrite, not a correction
    # Widen each change to whole words; changes whose words touch are one issue.
    merged: list[tuple[int, int, list[tuple[int, int, str]]]] = []
    for h in hunks:
        x0, x1 = _widen(body, h[0], h[1])
        if merged and x0 <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], x1), [*merged[-1][2], h])
        else:
            merged.append((x0, x1, [h]))
    protected = find_protected(text)
    out: list[Issue] = []
    for x0, x1, new in _pieces(body, merged):
        old = body[x0:x1]
        if old == new or not minimal_edit(old, new) or not _no_swap(old, new):
            continue
        if _PLACEHOLDER.search(old) or _PLACEHOLDER.search(new):
            continue  # protected content is never retyped
        start, end = x0 + lead, x1 + lead
        if any(start < p.end and p.start < end for p in protected):
            continue
        kind = _kind(old, new)
        rule = f"llm:{kind}"
        out.append(Issue(f"llm:{rule}:{start}:{end}", "llm", rule, MESSAGES[kind],
                         start, end, (new,), False))
    return out
