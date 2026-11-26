import re

from glance.grammar.issues import Issue, apply_edit
from glance.protocol import Rect
from glance.ui.strip_model import (
    COMPACT_CHARS,
    Segment,
    StripView,
    scroll_max_h,
    strip_fix,
    strip_view,
)


def _find(text, word, nth=0):
    m = list(re.finditer(rf"(?<!\w){re.escape(word)}(?!\w)", text))[nth]
    return m.start(), m.end()


def harper(text, word, repl, *, safe=True, rule="Rule", nth=0):
    start, end = _find(text, word, nth)
    return Issue(f"harper:{rule}:{start}:{end}", "harper", rule, "msg", start, end,
                 (repl,) if isinstance(repl, str) else repl, safe)


def llm(text, word, repl):
    start, end = _find(text, word)
    return Issue(f"llm:llm:grammar:{start}:{end}", "llm", "llm:grammar", "msg", start, end,
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


def test_the_whole_text_every_paragraph_and_the_whole_field_is_replaced():
    text = "teh first line here\nwe needs more\nlast i line"
    issues = [harper(text, "teh", "the"), harper(text, "needs", "need"),
              harper(text, "i", "I")]
    fix = strip_fix(text, text.index("more"), issues)
    assert fix is not None and fix.n_fixes == 3  # the badge counts all of them
    corrected = "the first line here\nwe need more\nlast I line"
    assert "".join(s.text for s in fix.segments) == corrected
    assert (fix.edit.start, fix.edit.end) == (0, len(text))  # the whole field, one step
    assert apply_edit(text, fix.edit) == corrected
    view = strip_view(fix)
    assert not view.compact and view.text == corrected and view.n_fixes == 3


def test_a_long_text_is_compact_with_its_changed_sentences_only():
    fine = "This sentence is fine and has nothing to fix at all. "
    text = fine * 3 + "We needs it now. " + fine * 4 + "\n\n" + fine * 2 + "And i agree. " + fine
    issues = [harper(text, "needs", "need"), harper(text, "i", "I")]
    fix = strip_fix(text, 0, issues)
    view = strip_view(fix)
    assert len(view.text) > COMPACT_CHARS and view.compact
    assert view.text == apply_edit(text, fix.edit).strip()  # "Use" types the whole text
    assert (fix.edit.start, fix.edit.end) == (0, len(text))
    assert "".join(s.text for s in view.segments) == "… We need it now. … And I agree. …"
    assert [s.text for s in view.segments if s.changed] == ["need", "I"]
    assert view.summary == "2 fixes across the message" and view.n_fixes == 2
    assert view.shown == view.segments
    expanded = StripView(view.segments, 2, expanded=True, full=view.full)
    assert expanded.shown == fix.segments  # "⋯": the whole corrected text


def test_a_long_changed_sentence_is_cut_to_its_change():
    long = ("and so " * 40) + "we needs it " + ("and so " * 40)
    fix = strip_fix(long, 0, [harper(long, "needs", "need")])
    view = strip_view(fix)
    shown = "".join(s.text for s in view.segments)
    assert shown.startswith("… ") and shown.endswith(" …") and "we need it" in shown
    assert len(shown) < 140


def test_scroll_height_is_a_share_of_the_monitor():
    mons = [Rect(0, 0, 1920, 1080), Rect(1920, 0, 1280, 2000)]
    assert scroll_max_h(mons, 2000, 10) == 800
    assert scroll_max_h(mons) == 432


def test_overlapping_fixes_first_wins():
    issues = [harper(TEXT, "its fine", "it's fine"), llm(TEXT, "its", "it is"),
              llm(TEXT, "fine", "good")]
    fix = strip_fix(TEXT, 0, issues)
    # by position, then length: "its fine" overlaps "its" and waits; "fine" does not
    assert fix.n_fixes == 2
    assert apply_edit(TEXT, fix.edit) == "hey team i think it is good but we needs the tests fixed"


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
    assert v.can_use and v.text == "a B" and not v.empty and not v.compact
    assert not StripView(note="Reads blunt").empty
    assert not StripView(note="Reads blunt").can_use
