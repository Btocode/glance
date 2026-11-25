import re

from glance.grammar.issues import Issue, apply_edit
from glance.ui.strip_model import MAX_UNIT_CHARS, Segment, StripView, strip_fix, unit_around


def _find(text, word, nth=0):
    m = list(re.finditer(rf"(?<!\w){re.escape(word)}(?!\w)", text))[nth]
    return m.start(), m.end()


def harper(text, word, repl, *, safe=True, rule="Rule", nth=0):
    start, end = _find(text, word, nth)
    return Issue(f"harper:{rule}:{start}:{end}", "harper", rule, "msg", start, end,
                 (repl,) if isinstance(repl, str) else repl, safe)


def llm(text, word, repl):
    start, end = _find(text, word)
    return Issue(f"llm:x:{start}:{end}", "llm", "x", "msg", start, end,
                 (repl,) if repl is not None else (), False)


TEXT = "hey team i think its fine but we needs the tests fixed"


def test_safe_fixes_and_ai_replacements_make_one_sentence():
    issues = [harper(TEXT, "i", "I"), harper(TEXT, "needs", "need"),
              llm(TEXT, "its", "it's"),
              harper(TEXT, "hey", ("Hey", "Hay"), safe=False)]  # not safe: left alone
    fix = strip_fix(TEXT, len(TEXT), issues)
    assert fix is not None and fix.n_fixes == 3
    assert "".join(s.text for s in fix.segments) == \
        "hey team I think it's fine but we need the tests fixed"
    assert [s.text for s in fix.segments if s.changed] == ["I", "it's", "need"]
    assert apply_edit(TEXT, fix.edit) == "hey team I think it's fine but we need the tests fixed"


def test_nothing_to_fix_is_none():
    assert strip_fix(TEXT, 3, []) is None
    assert strip_fix(TEXT, 3, [harper(TEXT, "hey", ("Hey", "Hay"), safe=False)]) is None
    assert strip_fix(TEXT, 3, [llm(TEXT, "its", None)]) is None
    assert strip_fix("", 0, []) is None


def test_only_the_paragraph_around_the_caret():
    text = "teh first line here\nwe needs more\nlast i line"
    issues = [harper(text, "teh", "the"), harper(text, "needs", "need"),
              harper(text, "i", "I")]
    at = text.index("more")
    fix = strip_fix(text, at, issues)
    assert fix is not None and fix.n_fixes == 1
    assert "".join(s.text for s in fix.segments) == "we need more"
    assert apply_edit(text, fix.edit) == "teh first line here\nwe need more\nlast i line"


def test_a_long_paragraph_is_cut_to_the_sentence_around_the_caret():
    first = "This is a sentence that goes on and on. " * 8
    text = first + "We needs it now. And more here."
    assert len(text) > MAX_UNIT_CHARS
    start, end = unit_around(text, text.index("now"))
    assert text[start:end] == "We needs it now."
    fix = strip_fix(text, text.index("now"), [harper(text, "needs", "need")])
    assert "".join(s.text for s in fix.segments) == "We need it now."


def test_overlapping_fixes_first_wins():
    issues = [harper(TEXT, "its fine", "it's fine"), llm(TEXT, "its", "it is"),
              llm(TEXT, "fine", "good")]
    fix = strip_fix(TEXT, 0, issues)
    # by position, then length: "its fine" overlaps "its" and waits; "fine" does not
    assert fix.n_fixes == 2 and fix.edit.replacement == "it is good"


def test_never_spans_or_touches_a_protected_entity():
    text = "teh note for @sam about teh plan"
    issues = [harper(text, "teh", "the"), harper(text, "teh", "the", nth=1)]
    fix = strip_fix(text, len(text), issues)  # the caret is at the end: the second run
    assert fix.n_fixes == 1
    assert fix.edit.start > text.index("@sam") + 4
    assert "".join(s.text for s in fix.segments) == "teh note for @sam about the plan"
    fix = strip_fix(text, 0, issues)
    assert fix.edit.end < text.index("@sam")
    at = text.index("@sam")
    assert strip_fix(text, 0, [harper(text, "@sam", "Sam")]) is None
    assert at > 0


def test_view_properties():
    assert StripView().empty and not StripView().can_use
    v = StripView((Segment("a ", False), Segment("B", True)), 1)
    assert v.can_use and v.text == "a B" and not v.empty
    assert not StripView(note="Reads blunt").empty
    assert not StripView(note="Reads blunt").can_use
