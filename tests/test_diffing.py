from itertools import pairwise

from glance.diffing import DiffOp, diff_markup, word_diff


def test_word_diff():
    assert word_diff("ok do it now", "ok please do it") == (
        DiffOp("eq", "ok "), DiffOp("ins", "please "), DiffOp("eq", "do it"),
        DiffOp("del", " now"))


def test_word_diff_identical_and_empty():
    assert word_diff("same text.", "same text.") == (DiffOp("eq", "same text."),)
    assert word_diff("", "") == ()
    assert word_diff("", "new words") == (DiffOp("ins", "new words"),)
    assert word_diff("old words", "") == (DiffOp("del", "old words"),)


def test_word_diff_replacement_is_delete_then_insert():
    assert word_diff("send teh report", "send the report") == (
        DiffOp("eq", "send "), DiffOp("del", "teh"), DiffOp("ins", "the"),
        DiffOp("eq", " report"))


def test_word_diff_tokens_are_words_spaces_and_punctuation_runs():
    # "now!!" is a word and a punctuation run: only the punctuation changes.
    assert word_diff("now!!", "now.") == (
        DiffOp("eq", "now"), DiffOp("del", "!!"), DiffOp("ins", "."))
    # A changed word never splits inside the word.
    assert word_diff("colour", "color") == (DiffOp("del", "colour"), DiffOp("ins", "color"))


def test_word_diff_round_trips_both_texts():
    old, new = "Hi @sam,\n  can u send\tit?", "Hello @sam — could you send it, please?"
    ops = word_diff(old, new)
    assert "".join(o.text for o in ops if o.kind != "ins") == old
    assert "".join(o.text for o in ops if o.kind != "del") == new
    assert all(a.kind != b.kind for a, b in pairwise(ops))  # adjacent ops are merged


def test_diff_markup_escapes_the_users_text():
    ops = (DiffOp("eq", "a <b> & "), DiffOp("del", "</span>"), DiffOp("ins", "x > y"))
    out = diff_markup(ops)
    assert out.startswith("a &lt;b&gt; &amp; <span")
    assert "&lt;/span&gt;" in out and "x &gt; y" in out
    assert out.count("<span") == 2 == out.count("</span>")
