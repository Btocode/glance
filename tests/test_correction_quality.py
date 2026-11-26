"""Correction quality: which fixes the corrected sentence may make, local and AI, in the
minimal and the fluent mode (`[general] correction`)."""
from __future__ import annotations

from glance.backends.base import Analysis, Ctx
from glance.backends.corrected import corrected_issues, fluent_ok
from glance.backends.prompts import analyze_system
from glance.config import load_config
from glance.grammar.corrections import (
    combine_sources,
    confident_spelling,
    dialect_variant,
    mechanical_fix,
    minimal_edit,
    same_stem,
    strip_replacement,
    swap_ok,
)
from glance.grammar.filters import code_like_spans
from glance.grammar.issues import Issue
from glance.ui.strip_model import strip_fix
from tests import test_ai_analyze as ai_t


def harper(text, word, rule, *repl, safe=None, at=0):
    start = text.index(word, at)
    return Issue(f"harper:{rule}:{start}", "harper", rule, "m", start, start + len(word),
                 tuple(repl), len(repl) == 1 if safe is None else safe)


def applied(text, issues, ai_corrects=False):
    fix = strip_fix(text, len(text), combine_sources(text, issues, ai_corrects))
    return text if fix is None else text[:fix.edit.start] + fix.edit.replacement + \
        text[fix.edit.end:]


def fixes(text, corrected, mode="minimal"):
    return [(text[i.start:i.end], i.replacements[0], i.rule)
            for i in corrected_issues(text, corrected, mode)]


# ---------------------------------------------------------------- word knowledge

def test_swaps_that_are_corrections_and_ones_that_are_not():
    assert swap_ok("dont", "doesn't") and swap_ok("Their", "There") and swap_ok("your", "you're")
    assert swap_ok("which", "who") and swap_ok("crash", "crashes") and swap_ok("sal", "all")
    assert not swap_ok("plan", "idea") and not swap_ok("handle", "manage")
    assert same_stem("fast", "faster") and same_stem("stop", "stopped")
    assert not same_stem("plan", "place")
    assert dialect_variant("colour", "color") and dialect_variant("organised", "organized")
    assert not dialect_variant("cat", "cot")


def test_minimal_edit_takes_grammar_words_but_never_flips_a_negation():
    assert minimal_edit("would have known", "had known")
    assert not minimal_edit("I can do it", "I can't do it")
    assert not minimal_edit("we need a fix", "a fix is required")


# ---------------------------------------------------------------- local fixes

def test_confident_spelling_leaves_chat_words_dialect_and_bad_splits_alone():
    def spell(text, word, *repl):
        return confident_spelling(text, harper(text, word, "SpellCheck", *repl, safe=False))
    assert spell("Thx, appreciate it", "Thx", "The", "That") is None
    assert spell("the colour is off", "colour", "color", "colony") is None
    assert spell("send the adress", "adress", "a dress", "address") is None
    assert spell("We werent told", "werent", "we rent", "weren't") == "weren't"
    assert spell("if the link dosent work", "dosent", "do sent", "doesn't") == "doesn't"
    assert spell("Youre right", "Youre", "Your", "You're") == "You're"
    assert spell("can u check", "u", "U", "up") is None


def test_style_rules_are_not_typed_by_the_strip():
    t = "FYI the config is out of date"
    assert strip_replacement(t, harper(t, "FYI", "ForYourInformation", "For your information")) \
        is None
    assert strip_replacement(t, harper(t, "config", "ExpandConfiguration", "configuration")) \
        is None
    assert strip_replacement(t, harper(t, "out of date", "OutOfDate", "out-of-date")) is None


def test_code_like_words_are_protected():
    t = "the bug is in parse_config(), see ~/.config/x.toml or .gitignore, 60 req/min. useEffect"
    got = [t[s.start:s.end] for s in code_like_spans(t)]
    assert got == ["parse_config(", "~/.config/x.toml", ".gitignore", "req/min", "useEffect"]
    assert code_like_spans("Mr. Khan said Wi-Fi is down, don't worry") == []


def test_a_space_before_punctuation_moves_after_it():
    t = "That works for me .See you then"
    assert applied(t, [harper(t, " ", "Spaces", "", at=16)]) == "That works for me. See you then"
    t = "add them to .gitignore"
    assert applied(t, [harper(t, " ", "Spaces", "", at=11)]) == t


# ---------------------------------------------------------------- AI and local together

def llm(text, word, new, kind="spelling", at=0):
    start = text.index(word, at)
    return Issue(f"llm:llm:{kind}:{start}", "llm", f"llm:{kind}", "m", start,
                 start + len(word), (new,), False)


def test_local_mechanical_fixes_survive_where_the_ai_left_the_text_alone():
    t = "hey can u send teh file , i need it"
    local = [harper(t, "hey", "SentenceCapitalization", "Hey"),
             harper(t, " ,", "Spaces", ","),
             harper(t, "i", "CapitalizePersonalPronouns", "I", at=26),
             harper(t, "teh", "SpellCheck", "the", "tea", safe=False)]
    ai = [llm(t, "teh", "the")]
    assert applied(t, local + ai, ai_corrects=True) == "Hey can u send the file, I need it"
    # where the AI changed the same text, it wins
    ai = [llm(t, "hey", "Hi", "grammar"), llm(t, "teh", "the")]
    assert applied(t, local + ai, ai_corrects=True).startswith("Hi can u")


def test_a_sentence_start_capital_joins_the_ai_fix_of_the_same_word():
    t = "Meeting at 3. dont be late"
    local = [harper(t, "dont", "SentenceCapitalization", "Dont")]
    assert applied(t, local + [llm(t, "dont", "don't")], ai_corrects=True) == \
        "Meeting at 3. Don't be late"


def test_only_mechanical_local_fixes_are_kept_beside_the_ai():
    t = "Me and sarah will handle it"
    pron = harper(t, "Me and sarah", "SubjectPronoun", "sarah and I")
    assert not mechanical_fix(t, pron)
    assert applied(t, [pron], ai_corrects=True) == t
    t = "We use pgvector"
    assert not mechanical_fix(t, harper(t, "pgvector", "SplitWords", "pg vector"))


# ---------------------------------------------------------------- the AI's corrected text

def test_minimal_mode_takes_homophones_and_agreement():
    got = fixes("He dont know if your coming", "He doesn't know if you're coming")
    assert ("dont", "doesn't", "llm:spelling") in got
    assert any(old == "your" and new == "you're" for old, new, _r in got)


def test_no_full_stop_the_writer_left_off():
    assert fixes("sounds good", "Sounds good.") == [("sounds", "Sounds", "llm:capitalization")]
    assert fixes("can you resend", "can you resend?") == [("resend", "resend?",
                                                           "llm:punctuation")]


def test_fluent_takes_a_reworded_sentence_minimal_does_not():
    t = "Since two weeks I am working on this bug."
    fluent = "I have been working on this bug for two weeks."
    assert corrected_issues(t, fluent) is None  # minimal: a rewrite, not a correction
    got = fixes(t, fluent, "fluent")
    assert got == [(t, fluent, "llm:rephrase")]
    assert applied(t, corrected_issues(t, fluent, "fluent"), True) == fluent


def test_fluent_never_takes_a_new_word_or_a_lost_idea():
    assert fluent_ok("This approach is more simple and more fast.",
                     "This approach is simpler and faster.")
    assert not fluent_ok("We can handle it tomorrow.", "We can deal with it tomorrow.")
    assert not fluent_ok("Send the report and the slides to Maria today.",
                         "Send it today.")
    assert not fluent_ok("It works on Linux.", "It doesn't work on Linux.")
    t = "We can handle it tomorrow."
    assert fixes(t, "We can deal with it tomorrow.", "fluent") == []


def test_fluent_keeps_protected_text():
    t = "how it works when @sam push to main?"
    got = fixes(t, "How does it work when @sam pushes to main?", "fluent")
    assert all("@sam" not in old for old, _n, _r in got)


# ---------------------------------------------------------------- config and prompt

def test_correction_setting(tmp_path):
    p = tmp_path / "config.toml"
    assert load_config(p).general.correction == "minimal"
    p.write_text('[general]\ncorrection = "fluent"\n')
    assert load_config(p).general.correction == "fluent"
    p.write_text('[general]\ncorrection = "wild"\n')
    assert load_config(p).general.correction == "minimal"


def test_the_prompt_follows_the_mode():
    ctx = Ctx("default", "neutral", "normal", "Slack")
    minimal = analyze_system(ctx)
    fluent = analyze_system(Ctx("default", "neutral", "normal", "Slack", correction="fluent"))
    assert "Never reword" in minimal and "rephrasing" not in minimal
    assert "rephrasing" in fluent and "never replace a correct word with a synonym" in fluent
    for s in (minimal, fluent):
        assert "first letter of every sentence" in s and '"i" -> "I"' in s


def test_fluent_mode_reaches_the_backend_and_labels_the_strip(tmp_path):
    answer = "send me the report now, I need it before the meeting starts."
    mode = ["fluent"]
    rig = ai_t.Rig(tmp_path, analysis=Analysis("neutral", False, None, (), corrected=answer),
                   correction=lambda: mode[0])
    rig.analyzed()
    assert rig.backend.calls[-1].ctx.correction == "fluent"
    rig.c.toggle_strip()
    assert rig.overlay.strip.label == "Fluent"
    assert rig.overlay.strip.text == answer
    s = ai_t.snap()
    fluent_key = rig.ai._key(s, rig.ai._resolve(s))
    mode[0] = "minimal"  # a result made in one mode is never shown in the other
    assert rig.ai._key(s, rig.ai._resolve(s)) != fluent_key


def test_no_capitalisation_fix_inside_a_banglish_sentence(tmp_path):
    from glance.daemon.main import build_issue_filter
    flt, _det = build_issue_filter(tmp_path / "dict.txt", tmp_path / "ignored.json")
    t = "ok ami dekhtesi. its fine"
    cap = Issue("llm:c", "llm", "llm:capitalization", "m", 0, 2, ("Ok",), False)
    its = Issue("llm:s", "llm", "llm:spelling", "m", 16, 19, ("It's",), False)
    assert flt.apply(t, [cap, its], "x") == [its]
