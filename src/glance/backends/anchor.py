"""Anchor LLM issues by quoted substring + occurrence index; never by character offset."""
from __future__ import annotations

from glance.backends.base import Analysis
from glance.grammar.issues import Issue
from glance.privacy.entities import find_protected


def resolve_anchor(text: str, quote: str, occurrence: int) -> tuple[int, int] | None:
    if not quote or occurrence < 1:
        return None
    pos = -len(quote)
    for _ in range(occurrence):
        pos = text.find(quote, pos + len(quote))
        if pos < 0:
            return None
    return pos, pos + len(quote)


def analysis_to_issues(text: str, a: Analysis) -> list[Issue]:
    protected = find_protected(text)
    out: list[Issue] = []
    for ai in a.issues:
        span = resolve_anchor(text, ai.quote, ai.occurrence)
        if span is None:
            continue
        start, end = span
        if any(start < p.end and p.start < end for p in protected):
            continue
        rule = f"llm:{ai.kind}"
        out.append(Issue(
            id=f"llm:{rule}:{start}:{end}", source="llm", rule=rule, message=ai.message,
            start=start, end=end,
            replacements=(ai.replacement,) if ai.replacement is not None else (), safe=False,
        ))
    return out
