"""Regression: the collapsed strip's corrected sentence only corrects, in the user's words.

A user typed the sentence below and the strip showed most typos unfixed, a fix applied but not
highlighted, "handle it" reworded to "handle this situation", and a badge that did not match.
The Harper fixture is real harper-ls 3.3.0 output for this exact text (diagnostics and their
code actions), so the conversion from LSP to issues runs as it does live.
"""
import json
from pathlib import Path

from glance.backends.base import Analysis, AnalysisIssue
from glance.grammar.corrections import confident_spelling, minimal_edit
from glance.grammar.harper import diagnostics_to_issues
from glance.grammar.issues import Issue, apply_edit
from glance.ui.strip_model import strip_fix
from tests.test_ai_analyze import Rig, snap

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "harper"
                      / "user_report_ai_sentence.json").read_text())
TEXT = FIXTURE["text"]
assert TEXT == ("what makes us more importent in the age of ai is likethe ability to coprehend "
                "things when someone is getting out fo control. we need a better way to handle it")
HARPER_ONLY = ("What makes us more important in the age of AI is like the ability to comprehend "
               "things when someone is getting out fo control. We need a better way to handle it")
EXPECTED = ("What makes us more important in the age of AI is like the ability to comprehend "
            "things when someone is getting out of control. We need a better way to handle it")


def harper_issues(text=TEXT):
    actions = {int(k): v for k, v in FIXTURE["actions"].items()}
    return diagnostics_to_issues(text, FIXTURE["diagnostics"], actions) if text == TEXT else []


def changed(view):
    return [s.text for s in view.segments if s.changed]


def test_harper_alone_fixes_every_confident_typo_and_highlights_each():
    fix = strip_fix(TEXT, len(TEXT), harper_issues())
    text = "".join(s.text for s in fix.segments)
    # "fo" stays: Harper's only ideas are "for", "FD", "F1"; two letters are too ambiguous.
    assert text == HARPER_ONLY and apply_edit(TEXT, fix.edit) == HARPER_ONLY
    assert [s.text for s in fix.segments if s.changed] == [
        "What", "important", "AI", "like the", "comprehend", "We"]
    assert fix.n_fixes == 6


def test_with_the_ai_analysis_corrections_only(tmp_path):
    analysis = Analysis("casual", False, None, (
        AnalysisIssue("fo", 1, "Spelling", "of", "spelling"),
        AnalysisIssue("importent", 1, "Spelling", "important", "spelling"),
        AnalysisIssue("handle it", 1, "Be specific", "handle this situation", "style"),
        # the same rewording, mislabelled: code does not take the label on trust
        AnalysisIssue("way to handle it", 1, "Grammar", "way to handle this situation",
                      "grammar"),
    ))
    rig = Rig(tmp_path, analysis=analysis)
    rig.harper.check = lambda text: (rig.harper.calls.append(text), harper_issues(text))[1]
    rig.analyzed(snap(text=TEXT))
    calls = len(rig.backend.calls)
    rig.c.toggle_strip()
    strip = rig.overlay.strip
    assert strip.text == EXPECTED
    assert changed(strip) == ["What", "important", "AI", "like the", "comprehend", "of", "We"]
    assert strip.n_fixes == 7 and rig.overlay.status.badge == "7"
    # the style suggestion is still offered, in the drawer's list, not in the sentence
    assert any(i.rule == "llm:style" and i.replacements == ("handle this situation",)
               for i in rig.c.issues)
    rig.c.use_strip()
    (req,) = rig.c._capture.applies
    assert apply_edit(TEXT, req.edit) == EXPECTED
    assert len(rig.backend.calls) == calls  # no extra AI call for the strip


def test_confident_spelling_rules():
    def spell(text, word, repl):
        start = text.index(word)
        return confident_spelling(text, Issue("h", "harper", "SpellCheck", "m", start,
                                              start + len(word), repl, False))
    assert spell("so importent", "importent", ("important", "impotent")) == "important"
    assert spell("the ai age", "ai", ("aim", "air", "AI")) == "AI"
    assert spell("out fo it", "fo", ("for", "FD")) is None  # too short to guess
    assert spell("ask Zorblax now", "Zorblax", ("Zorbas",)) is None  # a name, mid-sentence
    assert spell("the NASDQ feed", "NASDQ", ("NASDAQ",)) is None  # an acronym
    assert spell("Teh cat", "Teh", ("The", "Th", "Tech")) == "The"
    assert spell("a cuold be", "cuold", ("could", "cold")) == "could"
    assert spell("I lke it", "lke", ("bike", "like")) is None  # top pick changes the letter


def test_minimal_edit_tells_a_correction_from_a_rewording():
    assert minimal_edit("fo", "of") and minimal_edit("likethe", "like the")
    assert minimal_edit("its", "it's") and minimal_edit("they is", "they are")
    assert minimal_edit("going store", "going to the store")
    assert not minimal_edit("handle it", "handle this situation")
    assert not minimal_edit("it", "this situation")
    assert not minimal_edit("we need a fix", "a fix is required")
