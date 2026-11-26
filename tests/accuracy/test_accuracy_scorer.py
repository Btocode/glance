"""The accuracy bank's scorer and corpus: pure, no Harper, no AI."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import accuracy_harness as H


def _case(inp, minimal=None, fluent=None, tags=("t",), alt=()):
    minimal = inp if minimal is None else minimal
    return H.Case("x", inp, minimal, minimal if fluent is None else fluent, tuple(tags), tuple(alt))


def test_edits_are_word_level_and_count_spaces():
    assert H.edits("a b", "a b") == set()
    assert H.edits("teh cat", "the cat") == {(0, 1, "the")}
    assert H.edits("hi , you", "hi, you") == {(1, 2, "")}
    # two neighbouring fixes are two edits
    assert len(H.edits("i dont", "I don't")) == 2


def test_score_exact_and_f1():
    c = _case("teh cat sat", "the cat sat")
    s = H.score_case(c, "the cat sat", "minimal")
    assert (s.exact, s.tp, s.fp, s.fn) == (True, 1, 0, 0)
    s = H.score_case(c, "teh cat sat", "minimal")
    assert (s.exact, s.tp, s.fp, s.fn) == (False, 0, 0, 1)
    s = H.score_case(c, "the dog sat", "minimal")
    assert (s.exact, s.tp, s.fp, s.fn) == (False, 1, 1, 0)
    assert s.false_correction


def test_alternatives_and_modes():
    c = _case("a b", "a b", "a. B", alt=("a, b",))
    assert H.score_case(c, "a, b", "minimal").exact
    assert H.score_case(c, "a. B", "fluent").exact
    assert not H.score_case(c, "a b", "fluent").exact


def test_aggregate_per_tag():
    a = H.CaseScore("1", ("x",), True, 1, 0, 0, "", "")
    b = H.CaseScore("2", ("x", "y"), False, 0, 1, 1, "", "")
    r = H.aggregate([a, b])
    assert next(iter(r)) == "ALL"
    assert r["x"]["n"] == 2 and r["x"]["exact"] == 0.5
    assert r["x"]["precision"] == 0.5 and r["x"]["recall"] == 0.5
    assert r["y"]["false_correction"] == 1.0
    assert "falsecorr" in H.table(r)


def test_batches_keep_banglish_apart_and_split_back():
    cases = [_case(f"t{i}", tags=("banglish",) if i % 5 == 0 else ("chat",)) for i in range(23)]
    groups = H.batches(cases, 8, frozenset({"banglish"}))
    assert sum(len(g) for g in groups) == 23
    assert all(len({"banglish" in c.tags for c in g}) == 1 for g in groups)
    assert max(len(g) for g in groups) <= 8
    g = groups[0]
    assert H.split_batch(g, "\n\n".join(c.input.upper() for c in g)) == \
        [c.input.upper() for c in g]
    assert H.split_batch(g, "one blob") == [None] * len(g)


def test_corpus_is_well_formed():
    cases = H.load_corpus()
    assert len(cases) >= 300
    tags = {t for c in cases for t in c.tags}
    for needed in ("correct", "protected", "banglish", "typo", "apostrophe", "caps",
                   "spacing", "agreement", "homophone", "runon", "missing_word", "grammar",
                   "chat", "email", "prompt", "review"):
        assert needed in tags, needed
    for c in cases:
        assert "\n\n" not in c.input, c.id  # drafts are joined by blank lines
        assert c.input.strip() == c.input, c.id
        if "correct" in c.tags:
            assert c.minimal == c.input == c.fluent, c.id
    # ids are unique and the file is one JSON object per line
    lines = H.CORPUS.read_text(encoding="utf-8").splitlines()
    assert len({json.loads(x)["id"] for x in lines if x.strip()}) == len(cases)


def test_split_batch_when_blank_lines_were_closed_up():
    cases = [_case("a b"), _case("c\nd"), _case("e")]
    assert H.split_batch(cases, "A b\nC\nd\nE") == ["A b", "C\nd", "E"]
    assert H.split_batch(cases, "A b\nC d\nE") == [None] * 3


def test_minimal_accepts_the_fluent_text_but_not_the_reverse():
    c = _case("i tried it didnt work", "I tried it didn't work", "I tried it. It didn't work")
    assert H.score_case(c, "I tried it. It didn't work", "minimal").exact
    assert not H.score_case(c, "I tried it didn't work", "fluent").exact
