"""What the default corrected sentence may change: corrections only, never rewording.

The collapsed strip shows the user's own sentence with its spelling, grammar, punctuation and
capitalisation fixed, in their words and tone. Two sources feed it:

- Harper: its one-replacement (safe) fixes, plus the spelling fixes it is confident about
  (`confident_spelling`): a word split ("likethe" -> "like the"), a case-only fix ("ai" ->
  "AI"), a lone suggestion, or a top suggestion that is a near miss of the typed word.
- The AI analysis: only issues of a correction kind (`CORRECTION_KINDS`) whose replacement is a
  minimal edit of the quoted text (`minimal_edit`). Clarity, style, wording and tone
  suggestions stay in the drawer's list.
"""
from __future__ import annotations

import difflib
import re

from glance.grammar.issues import Issue

SPELL_RULE = "SpellCheck"
SPLIT_RULE = "SplitWords"
# AI issue kinds that only correct the text (anything else is a suggestion, not a fix).
CORRECTION_KINDS = frozenset({
    "spelling", "grammar", "punctuation", "capitalization", "agreement", "missing_word"})
# AI issue kinds that may reword: offered in the drawer, never in the corrected sentence.
SUGGESTION_KINDS = ("clarity", "style", "wording", "tone")

_WORD = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)*")
_TOKEN = re.compile(r"\w+(?:['’]\w+)*|[^\w\s]")


def osa_distance(a: str, b: str) -> int:
    """Edit distance counting an adjacent transposition as one edit."""
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[len(b)]


def _sentence_start(text: str, pos: int) -> bool:
    before = text[:pos].rstrip()
    return not before or before[-1] in ".!?…\n"


def confident_spelling(text: str, issue: Issue) -> str | None:
    """The replacement for a Harper spelling (or word-split) issue that is safe to apply
    without asking, or None. Names, acronyms and very short words are left alone (a
    two-letter typo has too many readings); the personal dictionary, protected entities and
    Banglish never get here (the issue filter drops them first)."""
    if issue.source != "harper" or issue.rule not in (SPELL_RULE, SPLIT_RULE):
        return None
    word = text[issue.start:issue.end]
    repl = issue.replacements
    if not repl or not _WORD.fullmatch(word):
        return None
    low = word.lower()
    for r in repl:  # "likethe" -> "like the"
        if " " in r and r.replace(" ", "").lower() == low:
            return r
    if issue.rule != SPELL_RULE:
        return None
    if word.isupper() or any(c.isupper() for c in word[1:]):
        return None  # an acronym or a camelCase name
    if word[0].isupper() and not _sentence_start(text, issue.start):
        return None  # a capitalised word mid-sentence: likely a name
    for r in repl:  # "ai" -> "AI"
        if r != word and r.lower() == low:
            return r
    if len(word) < 3:
        return None
    first = repl[0]
    if not _WORD.fullmatch(first):
        return None
    if len(repl) == 1:
        return first
    if first[0].lower() == low[0] and osa_distance(low, first.lower()) <= 2:
        return first
    return None


def ai_kind(issue: Issue) -> str:
    return issue.rule.removeprefix("llm:")


def _close(a: str, b: str) -> bool:
    a, b = a.lower(), b.lower()
    return a == b or osa_distance(a, b) <= max(1, min(2, len(a) // 3))


def minimal_edit(quote: str, replacement: str) -> bool:
    """True when `replacement` corrects `quote` rather than rewording it: the words it drops
    or swaps are near misses of the words it puts in (a typo, an inflection, a split or a
    join), plus at most two words added, dropped or swapped one for one (a missing or doubled
    word, agreement: "is" -> "are")."""
    a, b = _TOKEN.findall(quote), _TOKEN.findall(replacement)
    if a == b:
        return quote != replacement  # whitespace only
    sm = difflib.SequenceMatcher(a=[t.lower() for t in a], b=[t.lower() for t in b],
                                 autojunk=False)
    budget = 2
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        old, new = a[i1:i2], b[j1:j2]
        if op == "equal" or "".join(old).lower() == "".join(new).lower():
            continue  # unchanged but for case, or a split or a join
        if op == "replace" and abs(len(old) - len(new)) <= 1 and all(
                any(_close(o, n) for n in new) for o in old if o.isalnum()):
            continue  # near misses ("its" -> "it is")
        budget -= len(old) if op == "replace" and len(old) == len(new) else len(old) + len(new)
        if budget < 0:
            return False
    return True


def ai_correction(text: str, issue: Issue) -> str | None:
    """The replacement of an AI-analysis issue that may go into the corrected sentence."""
    if issue.source != "llm" or not issue.replacements:
        return None
    if ai_kind(issue) not in CORRECTION_KINDS:
        return None
    r = issue.replacements[0]
    return r if minimal_edit(text[issue.start:issue.end], r) else None


def strip_replacement(text: str, issue: Issue) -> str | None:
    """What the corrected sentence puts in place of `issue`'s span, or None to leave it."""
    if issue.source == "harper":
        if issue.safe and issue.replacements:
            return issue.replacements[0]
        return confident_spelling(text, issue)
    if issue.source == "llm":
        return ai_correction(text, issue)
    return None
