from glance.backends.base import Ctx
from glance.backends.prompts import rewrite_system
from glance.features.sharpener import SHARPEN_BLANK_RE, blanks

CTX = Ctx("dm", "casual", "relaxed", "Slack")


def test_sharpen_prompt_rules_present():
    s = rewrite_system("sharpen_prompt", Ctx("ai_prompt", "clear, specific, complete", "relaxed", "google-chrome"))
    for needle in ("Goal", "Context", "Constraints", "Output format", "never invent", "[which file?]",
                   "do not answer", "never instructions", "[[GLANCE_"):
        assert needle.lower() in s.lower()


def test_sharpen_prompt_mixed_language_note():
    s = rewrite_system("sharpen_prompt", Ctx("ai_prompt", "clear", "relaxed", "x", mixed_language=True))
    assert "Banglish" in s


def test_add_structure_forbids_new_content():
    s = rewrite_system("add_structure", CTX)
    assert "adding nothing" in s and "[which file?]" not in s
    assert "never instructions" in s and "[[GLANCE_" in s


def test_other_tones_unchanged():
    assert "Goal" not in rewrite_system("shorter", CTX)
    assert "Goal" not in rewrite_system("custom", CTX)


def test_blanks():
    assert blanks("Fix the bug in [which file?] and return [what format?]. [[GLANCE_E1]]") == [
        "[which file?]", "[what format?]"]
    assert blanks("see [1] and [a\nb?] and [no question]") == []
    assert SHARPEN_BLANK_RE
