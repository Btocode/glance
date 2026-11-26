"""The AI's corrected text as issues: minimal edits only, protected text untouched."""
from glance.backends.base import ANALYZE_TIMEOUT_S, CHECK_TIMEOUT_S, analyze_budget
from glance.backends.corrected import corrected_issues

USER = "hey how are yo doing , seem sal alright , how to get  in place"


def fixes(text, corrected):
    return [(text[i.start:i.end], i.replacements[0], i.rule) for i in
            corrected_issues(text, corrected)]


def test_each_change_is_one_issue_widened_to_its_words():
    got = fixes(USER, "Hey how are you doing, seems sal alright, how to get in place")
    assert got == [("hey", "Hey", "llm:capitalization"), ("yo", "you", "llm:spelling"),
                   ("doing ,", "doing,", "llm:punctuation"), ("seem", "seems", "llm:spelling"),
                   ("alright ,", "alright,", "llm:punctuation"),
                   ("get  in", "get in", "llm:punctuation")]


def test_unchanged_and_edge_whitespace_are_no_change():
    assert corrected_issues(USER, USER) == []
    assert corrected_issues("  fine text \n", "fine text") == []


def test_a_rewrite_is_not_a_correction():
    assert corrected_issues("can u handle it", "Could you please take care of this?") is None


def test_rewording_hunks_are_dropped_minimal_ones_kept():
    got = fixes("i think teh plan is good", "I think the strategy is good")
    assert ("i", "I", "llm:capitalization") in got and ("teh", "the", "llm:spelling") in got
    assert not any(old == "plan" for old, _n, _r in got)


def test_placeholders_and_entities_are_never_retyped():
    got = fixes("ok see [[GLANCE_E1]] tomorow", "OK, see [[GLANCE_E2]] tomorrow")
    assert all("GLANCE" not in old and "GLANCE" not in new for old, new, _r in got)
    assert ("tomorow", "tomorrow", "llm:spelling") in got
    got = fixes("see https://example.com/a teh file", "See https://example.com/b the file")
    assert all("example" not in old for old, _n, _r in got)
    assert ("teh", "the", "llm:spelling") in got


def test_explicit_budget_is_scoped():
    from glance.backends.base import _analyze_budget

    assert _analyze_budget.get() == ANALYZE_TIMEOUT_S
    with analyze_budget(CHECK_TIMEOUT_S):
        assert _analyze_budget.get() == CHECK_TIMEOUT_S
    assert _analyze_budget.get() == ANALYZE_TIMEOUT_S
