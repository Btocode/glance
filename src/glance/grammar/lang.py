"""Per-sentence Banglish / non-English detection so local grammar never nags about it.

Detection is deliberately conservative: a sentence is non-English only on Bangla script or on
Banglish function words. Misspellings alone (sloppy English, jargon, names) never flip it.
"""
from __future__ import annotations

import re

from glance.grammar.issues import Issue
from glance.grammar.store import PersonalDictionary
from glance.privacy.entities import Span

_MARKER_WORDS = """
ami amra tumi tomra apni tui amar tomar apnar amader ki keno kothay kobe kivabe kemon eta ota
oita ekta ekhon ekhane okhane kintu karon tai jodi tahole ache achi acho achen nai nei hobe hoye
hoyeche hocche korbo korbe korte korchi korcho korlam koro koren kora bolo bolen bolchi jani
janina lagbe parbo parbe thik bhalo valo khub onek ektu aro shob kaj bhai apu dhonnobad accha
hae na abar amake tomake apnake kalke ajke ekhono jabo jacchi jabe ashbo ashchi dekhbo dekhchi
dekhbe dekhlam dekhtesi diyechi pathabo pathiye pathano korben korechi korlo koreche korche
hoise hoyni hoyeche lagche bujhte parchi parba shesh sokale tay bolche bollo deri pagol shobai
kal aj ta re ke naki dio hoy
"""
BANGLISH_MARKERS: frozenset[str] = frozenset(_MARKER_WORDS.split())

# Words that are also plain English or too generic: they count only next to a strong marker.
_WEAK = frozenset({"na", "ki", "tai", "kora", "ache", "kal", "aj", "ta", "re", "ke", "naki",
                   "dio", "hoy"})

_BENGALI = re.compile("[ঀ-৿]")
_WORD = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?", re.UNICODE)
_BOUNDARY = re.compile(r"[.?!]+(?=\s|$)|\n+")


def split_sentences(text: str) -> list[Span]:
    """Sentence spans (kind `sentence`) split on `.?!` before space/end and on newlines."""
    out: list[Span] = []

    def add(a: int, b: int) -> None:
        while a < b and text[a].isspace():
            a += 1
        while b > a and text[b - 1].isspace():
            b -= 1
        if a < b:
            out.append(Span(a, b, "sentence"))

    pos = 0
    for m in _BOUNDARY.finditer(text):
        add(pos, m.start())
        pos = m.end()
    add(pos, len(text))
    return out


def _marker_count(sentence: str) -> int:
    words = [w.casefold() for w in _WORD.findall(sentence)]
    strong = sum(1 for w in words if w in BANGLISH_MARKERS and w not in _WEAK)
    if not strong:
        return 0
    return strong + sum(1 for w in words if w in _WEAK)


def sentence_is_non_english(sentence: str, misspelled: int, words: int) -> bool:
    """True for Bangla script or Banglish markers; never on a misspelling ratio alone."""
    if _BENGALI.search(sentence):
        return True
    markers = _marker_count(sentence)
    if markers >= 2:
        return True
    if markers < 1 or words <= 0:
        return False
    ratio = misspelled / words
    return ratio >= 0.25 or (words >= 4 and ratio >= 0.6)


class LanguageDetector:
    def __init__(self, dictionary: PersonalDictionary) -> None:
        self._dictionary = dictionary

    def spans(self, text: str, issues: list[Issue]) -> list[Span]:
        """Spans (kind `lang`) of sentences that are not English."""
        spell = [i for i in issues if i.rule == "SpellCheck"
                 and not self._dictionary.contains(text[i.start:i.end])]
        out = []
        for s in split_sentences(text):
            sentence = text[s.start:s.end]
            misspelled = sum(1 for i in spell if s.start <= i.start and i.end <= s.end)
            if sentence_is_non_english(sentence, misspelled, len(_WORD.findall(sentence))):
                out.append(Span(s.start, s.end, "lang"))
        return out

    def is_mixed(self, text: str) -> bool:
        """Any non-English sentence by script/markers alone (no Harper results needed)."""
        return any(sentence_is_non_english(text[s.start:s.end], 0, 0)
                   for s in split_sentences(text))
