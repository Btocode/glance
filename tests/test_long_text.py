"""A long field: the AI gets `[ai] max_words` words around the caret, the strip says so,
local grammar still sees the whole text, and a rewrite only replaces the part it was sent."""
from dataclasses import replace

from glance.backends.base import Analysis, AnalysisIssue
from glance.grammar.issues import apply_edit
from tests.test_ai_analyze import Rig, snap

FINE = "Sentence number {} reads fine to me."  # seven words
TEH = AnalysisIssue("teh", 1, "Spelling", "the", "spelling")


def long_text(n=100, typo_at=50):
    return " ".join(FINE.format(i) if i != typo_at else "We send teh report now."
                    for i in range(n))  # ~700 words


def test_ai_sees_the_window_around_the_caret_and_the_strip_says_so(tmp_path):
    rig = Rig(tmp_path, analysis=Analysis("neutral", False, None, (TEH,)), max_words=lambda: 100)
    text = long_text()
    caret = text.index("teh")
    s = replace(snap(text=text), caret=caret)
    rig.analyzed(s)
    call = rig.backend.calls[-1]
    assert call.kind == "analyze" and len(call.text.split()) <= 100
    assert "teh" in call.text and call.text in text and call.text.endswith("me.")
    (issue,) = rig.llm()
    assert text[issue.start:issue.end] == "teh"  # at its place in the whole field
    assert rig.harper.calls[-1] == text  # the local check reads all of it
    rig.c.toggle_strip()
    strip = rig.overlay.strip
    assert strip.meta == f"Long text: AI checked {len(call.text.split())} of {len(text.split())} words " \
        "around your cursor"
    assert strip.n_fixes == 1 and strip.compact and strip.summary == "1 fix across the message"
    rig.c.use_strip()
    (req,) = rig.c._capture.applies
    assert (req.edit.start, req.edit.end) == (0, len(text))
    assert apply_edit(text, req.edit) == text.replace("teh", "the")


def test_a_short_text_goes_whole_without_a_notice(tmp_path):
    rig = Rig(tmp_path, analysis=Analysis("neutral", False, None, (TEH,)))
    text = "First paragraph is fine.\n\nWe send teh report now. Last one fine too."
    rig.analyzed(replace(snap(text=text), caret=3))
    assert rig.backend.calls[-1].text == text
    rig.c.toggle_strip()
    assert rig.overlay.strip.meta is None and rig.overlay.strip.n_fixes == 1


def test_a_rewrite_of_a_long_text_only_replaces_its_window(tmp_path):
    rig = Rig(tmp_path, max_words=lambda: 100)
    text = long_text()
    caret = text.index("teh")
    s = rig.analyzed(replace(snap(text=text), caret=caret))
    rig.ai.preview("friendlier")
    call = rig.backend.calls[-1]
    assert call.kind == "rewrite" and len(call.text.split()) <= 100 and call.text in text
    rig.ai.apply()
    (req,) = rig.c._capture.applies
    w0 = text.index(call.text)
    assert (req.edit.start, req.edit.end) == (w0, w0 + len(call.text))
    assert req.edit.replacement == "Thanks so much, will do!"
    assert s.text == text
