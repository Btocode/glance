"""Drop issues the user must never see: protected spans, ignored rules, dictionary words."""
from __future__ import annotations

from collections.abc import Callable, Sequence

from glance.grammar.issues import Issue
from glance.grammar.store import IgnoredRules, PersonalDictionary
from glance.privacy.entities import Span, find_protected, overlaps

SpanFilter = Callable[[str], list[Span]]
# Needs the issues too (e.g. language detection); suppresses only local (harper) issues.
IssueSpanFilter = Callable[[str, list[Issue]], list[Span]]


class IssueFilter:
    def __init__(self, dictionary: PersonalDictionary, ignored: IgnoredRules,
                 span_filters: list[SpanFilter],
                 issue_span_filters: Sequence[IssueSpanFilter] = ()) -> None:
        self._dictionary = dictionary
        self._ignored = ignored
        self._span_filters = [find_protected, *span_filters]
        self._issue_span_filters = list(issue_span_filters)

    @property
    def dictionary(self) -> PersonalDictionary:
        return self._dictionary

    @property
    def ignored(self) -> IgnoredRules:
        return self._ignored

    def apply(self, text: str, issues: list[Issue], wm_class: str,
              extra_ignored: frozenset[str] = frozenset()) -> list[Issue]:
        spans: list[Span] = []
        for f in self._span_filters:
            spans += f(text)
        local_spans: list[Span] = []
        for g in self._issue_span_filters:
            local_spans += g(text, issues)
        out = []
        for i in issues:
            if overlaps(spans, i.start, i.end):
                continue
            if i.source == "harper" and overlaps(local_spans, i.start, i.end):
                continue
            if i.rule in extra_ignored or self._ignored.is_ignored(i.rule, wm_class):
                continue
            if self._dictionary.contains(text[i.start:i.end]):
                continue
            out.append(i)
        return out
