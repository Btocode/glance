from glance.backends.anchor import analysis_to_issues, resolve_anchor
from glance.backends.base import Analysis, AnalysisIssue


def test_resolve_anchor():
    t = "teh cat and teh dog"
    assert resolve_anchor(t, "teh", 1) == (0, 3)
    assert resolve_anchor(t, "teh", 2) == (12, 15) and resolve_anchor(t, "teh", 3) is None
    assert resolve_anchor(t, "", 1) is None and resolve_anchor("aaa", "aa", 2) is None
    assert resolve_anchor(t, "teh", 0) is None


def _ai(quote, occ=1, repl="the", kind="grammar"):
    return AnalysisIssue(quote, occ, "msg", repl, kind)


def test_analysis_to_issues_drops_unresolved_and_protected():
    text = "teh cat @sam nope?"
    a = Analysis("neutral", False, None,
                 (_ai("teh"), _ai("nope", 2), _ai("@sam", 1, None, "tone"), _ai("cat", 1, None)))
    out = analysis_to_issues(text, a)
    assert [(i.start, i.end) for i in out] == [(0, 3), (4, 7)]
    first, second = out
    assert first.source == "llm" and first.rule == "llm:grammar" and first.safe is False
    assert first.replacements == ("the",) and first.message == "msg"
    assert second.replacements == () and second.id == "llm:llm:grammar:4:7"


def test_analysis_to_issues_drops_overlap_with_protected_partial():
    text = "see https://x.io/a now"
    a = Analysis("n", False, None, (_ai("x.io", 1), _ai("now", 1, "today", "clarity")))
    out = analysis_to_issues(text, a)
    assert [(i.start, i.end) for i in out] == [(19, 22)]
