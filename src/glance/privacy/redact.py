"""Secret detection and placeholder shielding: what reaches an AI backend is shielded text."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from itertools import accumulate, pairwise

from glance.privacy.entities import Span, find_protected

_B = r"(?<![A-Za-z0-9_-])"
_API_KEY = re.compile(
    _B + r"(?:sk-[A-Za-z0-9_-]{20,}|gh[pos]_[A-Za-z0-9]{20,}|github_" r"pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}|xox[abprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35}"
    r"|(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}|glpat-[A-Za-z0-9_-]{20,})"
    r"|\bBearer\s+\S{20,}"
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_PEM = re.compile(r"-----BEGIN [A-Z0-9 ]+-----.*?(?:-----END [A-Z0-9 ]+-----|\Z)", re.DOTALL)
_CARD_RUN = re.compile(r"\d(?:[ \t\r\n-]?\d)*")
_ASSIGN = re.compile(
    r"\b(?:password|passwd|pwd|secret|token|api[_-]?key)\s*[:=]\s*"
    r"(?:\"(?P<dq>[^\"\n]+)\"|'(?P<sq>[^'\n]+)'|(?P<bare>\S+))",
    re.IGNORECASE,
)
_TOKEN = re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/=_-]{20,}(?![A-Za-z0-9+/=_-])")
_HEX = re.compile(r"[0-9a-f]+")
_SHORT_ENTROPY = 3.7   # mixed-case tokens under 4.0 bits
_SHORT_SWITCHES = 0.5  # ... that change character class this often

# Overlap precedence, strongest first.
_RANK = {"pem": 0, "jwt": 1, "api_key": 2, "assignment": 3, "card": 4, "entropy": 5}


def _entropy(token: str) -> float:
    n = len(token)
    return -sum(c / n * math.log2(c / n) for c in Counter(token).values())


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _char_class(ch: str) -> str:
    return "u" if ch.isupper() else "l" if ch.islower() else "d" if ch.isdigit() else "o"


def _class_switches(tok: str) -> float:
    """How often neighbouring characters differ in class (upper, lower, digit, other).

    About 0.6 for a random mixed-case token, about 0.3 for a camelCase identifier, whose
    capitals each start a lowercase word.
    """
    pairs = pairwise(tok)
    return sum(_char_class(a) != _char_class(b) for a, b in pairs) / (len(tok) - 1)


def _is_entropy_secret(tok: str) -> bool:
    if not (any(c.isalpha() for c in tok) and any(c.isdigit() for c in tok)):
        return False
    if "/" in tok and not any(c.isupper() or c in "+=" for c in tok):
        return False  # a lowercase path (`/var/notes/budget_2024`), not base64
    e = _entropy(tok)
    if e >= 4.0 or (len(tok) >= 32 and _HEX.fullmatch(tok) is not None and e >= 3.0):
        return True
    # A short random token (20-23 characters) cannot reach 4.0 bits often: with so few
    # characters some repeat. Below 4.0 it must also look random rather than word-made.
    return (e >= _SHORT_ENTROPY and _class_switches(tok) >= _SHORT_SWITCHES
            and any(c.isupper() for c in tok) and any(c.islower() for c in tok))


def _card_spans(base: int, run: str) -> list[Span]:
    """Every Luhn-valid 13-19 digit window of whole digit groups in a run (overlaps merge, so a
    chance-valid window never hides the real card; over-redaction beats leaking).

    Luhn sums come from parity-split prefix sums, so each window check is O(1).
    """
    if len(run) < 13:
        return []
    spans = [m.span() for m in re.finditer(r"\d+", run)]
    n = len(spans)
    starts = [0]
    starts.extend(accumulate(e - s for s, e in spans))  # digit index where each group begins
    if starts[-1] < 13:
        return []
    digits = [int(ch) for ch in run if ch.isdigit()]
    dbl = [d * 2 - 9 if d > 4 else d * 2 for d in digits]
    # c[k][i]: prefix sum where a digit counts doubled iff its index has parity k, else plain
    c = [
        [0] + list(accumulate(dbl[i] if i % 2 == k else d for i, d in enumerate(digits)))
        for k in (0, 1)
    ]
    out: list[Span] = []
    covered = -1  # end of the last span found
    lo = hi = 0
    for i in range(n):
        s0 = starts[i]
        lo = max(lo, i)
        while lo < n and starts[lo + 1] - s0 < 13:
            lo += 1
        while lo < n and base + spans[lo][1] <= covered:
            lo += 1  # windows ending inside an already-found span cannot change the union
        hi = max(hi, lo)
        while hi < n and starts[hi + 1] - s0 <= 19:
            hi += 1
        for j in range(lo, hi):
            e = starts[j + 1]
            ck = c[e & 1]  # digit p is doubled when (e - 1 - p) is odd, i.e. p has parity of e
            if (ck[e] - ck[s0]) % 10 == 0:
                a0, b0 = base + spans[i][0], base + spans[j][1]
                if out and a0 <= covered:
                    out[-1] = Span(out[-1].start, max(covered, b0), "card")
                else:
                    out.append(Span(a0, b0, "card"))
                covered = out[-1].end
    return out


def find_secrets(text: str) -> list[Span]:
    """Secret spans, sorted and non-overlapping; overlaps merge, the strongest kind wins."""
    found: list[Span] = []
    for kind, pat in (("pem", _PEM), ("jwt", _JWT), ("api_key", _API_KEY)):
        found += [Span(m.start(), m.end(), kind) for m in pat.finditer(text)]
    for m in _ASSIGN.finditer(text):
        g = next(n for n in ("dq", "sq", "bare") if m.group(n) is not None)
        found.append(Span(m.start(g), m.end(g), "assignment"))
    for m in _CARD_RUN.finditer(text):
        found += _card_spans(m.start(), m.group())
    for m in _TOKEN.finditer(text):
        if _is_entropy_secret(m.group()):
            found.append(Span(m.start(), m.end(), "entropy"))
    found.sort(key=lambda s: (s.start, _RANK[s.kind]))
    merged: list[Span] = []
    for s in found:
        if merged and s.start < merged[-1].end:
            prev = merged[-1]
            kind = min(prev.kind, s.kind, key=_RANK.__getitem__)
            merged[-1] = Span(prev.start, max(prev.end, s.end), kind)
        else:
            merged.append(s)
    return merged


_PLACEHOLDER_OPEN = "[[GLANCE_"


@dataclass(frozen=True)
class Shielded:
    """`mapping` is (placeholder, original) in text order; `segments` backs `map_span`."""

    text: str
    mapping: tuple[tuple[str, str], ...]
    has_objects: bool
    # (shielded_start, shielded_end, original_start, original_end, is_placeholder)
    segments: tuple[tuple[int, int, int, int, bool], ...] = ()

    def map_span(self, start: int, end: int) -> tuple[int, int] | None:
        """Shielded offsets -> original offsets; None if the span touches a placeholder."""
        for s0, s1, _o0, _o1, ph in self.segments:
            if ph and start < s1 and end > s0:
                return None
        return self._pos(start), self._pos(end)

    def _pos(self, p: int) -> int:
        for s0, s1, o0, o1, ph in self.segments:
            if s0 <= p <= s1:
                if not ph:
                    return o0 + (p - s0)
                return o0 if p == s0 else o1
        return p


def shield(text: str) -> Shielded:
    secrets = find_secrets(text)
    entities = [
        e for e in find_protected(text)
        if not any(e.start < s.end and s.start < e.end for s in secrets)
    ]
    items = sorted(
        [(s.start, s.end, "S") for s in secrets] + [(e.start, e.end, "E") for e in entities]
    )
    counts = {"S": 0, "E": 0}
    out: list[str] = []
    mapping: list[tuple[str, str]] = []
    segments: list[tuple[int, int, int, int, bool]] = []
    pos = 0  # original offset
    cur = 0  # shielded offset
    for a, b, tag in items:
        if a > pos:
            out.append(text[pos:a])
            segments.append((cur, cur + a - pos, pos, a, False))
            cur += a - pos
        counts[tag] += 1
        ph = f"[[GLANCE_{tag}{counts[tag]}]]"
        out.append(ph)
        mapping.append((ph, text[a:b]))
        segments.append((cur, cur + len(ph), a, b, True))
        cur += len(ph)
        pos = b
    if pos < len(text):
        out.append(text[pos:])
        segments.append((cur, cur + len(text) - pos, pos, len(text), False))
    return Shielded("".join(out), tuple(mapping), "￼" in text, tuple(segments))


def unshield(text: str, s: Shielded) -> str | None:
    """Restore originals; None unless every placeholder occurs once, in order, and none is invented."""
    if text.count(_PLACEHOLDER_OPEN) != len(s.mapping):
        return None
    last = -1
    for ph, _orig in s.mapping:
        if text.count(ph) != 1:
            return None
        i = text.index(ph)
        if i < last:
            return None
        last = i
    # single pass so originals containing placeholder-like text are never re-expanded
    originals = dict(s.mapping)
    return re.sub(r"\[\[GLANCE_[SE]\d+\]\]", lambda m: originals[m.group()], text)


def can_apply_rewrite(s: Shielded, rich: bool = False) -> bool:
    """May a rewrite of the shielded text be typed back over the whole field?

    Never over embedded objects. In a rich editor (`rich`) never over any protected entity
    either: a mention, emoji, link or code span there is an object that typed text would
    flatten, even when the characters come out the same.
    """
    if s.has_objects:
        return False
    return not (rich and any(ph.startswith("[[GLANCE_E") for ph, _ in s.mapping))
