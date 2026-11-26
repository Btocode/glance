"""The in-process grammar engine: selection, the client wrapper, and the extension itself."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from glance.grammar import harper_native as hn
from glance.grammar.harper import HarperClient
from glance.grammar.harper_native import NativeHarperClient, engine_choice, make_client

needs_core = pytest.mark.skipif(not hn.available(), reason="glance_core is not built")


class FakeGrammar:
    def __init__(self, **kw):
        self.kw = kw
        self.calls: list[tuple] = []
        self.result: list[dict] = []
        self.boom: BaseException | None = None

    def check(self, text):
        self.calls.append(("check", text))
        if self.boom is not None:
            raise self.boom
        return self.result

    def load_dictionary(self, path):
        self.calls.append(("load", path))

    def set_words(self, words):
        self.calls.append(("set_words", tuple(words)))

    def set_disabled_rules(self, rules):
        self.calls.append(("disabled", tuple(rules)))


def lint(start, end, rule, repl, message="m"):
    return {"start": start, "end": end, "rule": rule, "message": message,
            "replacements": list(repl), "suggestions": list(repl)}


@pytest.fixture
def fake(tmp_path):
    made: list[FakeGrammar] = []

    def factory(**kw):
        made.append(FakeGrammar(**kw))
        return made[-1]

    client = NativeHarperClient(tmp_path / "dictionary.txt", grammar_factory=factory)
    return client, made


# ------------------------------------------------------------------ selection


def test_engine_choice_prefers_native_when_it_imports(monkeypatch):
    monkeypatch.setattr(hn, "_core", object())
    assert engine_choice({}) == "native"
    assert engine_choice({"GLANCE_GRAMMAR": "harper-ls"}) == "harper-ls"
    monkeypatch.setattr(hn, "_core", None)
    assert engine_choice({}) == "harper-ls"
    assert engine_choice({"GLANCE_GRAMMAR": "native"}) == "harper-ls"  # nothing to run


def test_make_client_falls_back_to_harper_ls(tmp_path, monkeypatch):
    monkeypatch.setattr(hn, "_core", None)
    c = make_client(tmp_path / "harper-ls", tmp_path / "d.txt", environ={})
    assert isinstance(c, HarperClient) and c.engine == "harper-ls"
    monkeypatch.setattr(hn, "_core", object())
    c = make_client(tmp_path / "harper-ls", tmp_path / "d.txt", environ={})
    assert isinstance(c, NativeHarperClient) and c.engine == "native"


# ------------------------------------------------------------------ the client


def test_not_alive_until_started_and_never_dies(fake):
    client, made = fake
    assert client.alive is False and client.check("x") == [] and client.degraded
    client.start()
    assert client.alive and len(made) == 1
    assert made[0].calls == [("check", hn.WARM_TEXT)]  # the slow first lookup, paid at start
    client.start()
    assert len(made) == 1
    client.stop()
    assert client.alive is False


def test_lints_become_issues_through_the_shared_builder(fake):
    client, made = fake
    client.start()
    made[0].result = [lint(0, 3, "The", ["The", "Th"]), lint(4, 5, "Rule", ["x"]),
                      lint(4, 5, "Rule", ["x"])]
    issues = client.check("Teh a")
    # "The" is a replacement, not a rule key: SpellCheck, as for harper-ls; duplicates dropped
    assert [(i.rule, i.start, i.end, i.replacements, i.safe) for i in issues] == [
        ("SpellCheck", 0, 3, ("The", "Th"), False), ("Rule", 4, 5, ("x",), True)]
    assert issues[1].id == "harper:Rule:4:5" and issues[1].source == "harper"


@pytest.mark.parametrize("exc", [RuntimeError("Harper panicked"), BaseException("pyo3 panic")])
def test_a_panic_is_logged_and_checks_nothing(fake, capsys, exc):
    client, made = fake
    client.start()
    made[0].boom = exc
    assert client.check("Teh") == [] and client.degraded
    assert "grammar_native_error" in capsys.readouterr().err
    made[0].boom = None
    made[0].result = [lint(0, 3, "SpellCheck", ["The"])]
    assert len(client.check("Teh")) == 1 and not client.degraded
    assert client.alive  # nothing to restart


def test_keyboard_interrupt_is_not_swallowed(fake):
    client, made = fake
    client.start()
    made[0].boom = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        client.check("x")


def test_dictionary_is_reloaded_when_the_file_changes(fake, tmp_path):
    client, made = fake
    words = tmp_path / "dictionary.txt"
    client.start()
    assert made[0].kw["dictionary"] is None
    g = made[0]
    client.check("a")
    assert ("load", str(words)) not in g.calls
    words.write_text("Zorblax\n")
    client.check("a")
    assert g.calls.count(("load", str(words))) == 1
    client.check("a")
    assert g.calls.count(("load", str(words))) == 1  # unchanged: not read again
    words.unlink()
    client.check("a")
    assert ("set_words", ()) in g.calls


def test_disabled_rules_reach_the_engine(fake):
    client, made = fake
    client.set_disabled_rules({"B", "A"})
    client.start()
    assert made[0].kw["disabled_rules"] == ["A", "B"]
    client.set_disabled_rules(set())
    assert ("disabled", ()) in made[0].calls


# ------------------------------------------------------------------ the extension


@needs_core
def test_extension_offsets_are_python_str_indices():
    import glance_core

    g = glance_core.Grammar()
    t = "😀🎉 x 𝓗𝓮 Thsi is fine."
    found = [d for d in g.check(t) if t[d["start"]:d["end"]] == "Thsi"]
    assert found and found[0]["rule"] == "SpellCheck" and "This" in found[0]["replacements"]


@needs_core
def test_extension_panic_is_a_runtime_error_and_the_engine_survives():
    import glance_core

    g = glance_core.Grammar()
    assert issubclass(glance_core.GrammarPanic, RuntimeError)
    with pytest.raises(glance_core.GrammarPanic):
        g._panic_for_test()
    assert any(d["rule"] == "SpellCheck" for d in g.check("A zqxjword."))


@needs_core
def test_extension_dictionary_and_words(tmp_path):
    import glance_core

    t = "A Zorblax and a Quuxify here."
    g = glance_core.Grammar(words=["Zorblax"])
    flagged = {t[d["start"]:d["end"]] for d in g.check(t)}
    assert "Zorblax" not in flagged and "Quuxify" in flagged
    assert g.add_word("Quuxify") is True and g.add_word("Quuxify") is False
    assert not {t[d["start"]:d["end"]] for d in g.check(t)} & {"Zorblax", "Quuxify"}
    with pytest.raises(ValueError):
        g.add_word("two words")
    f = tmp_path / "d.txt"
    f.write_text("Zorblax\n\n")
    assert g.load_dictionary(str(f)) == 1  # replaces the words: Quuxify is flagged again
    assert "Quuxify" in {t[d["start"]:d["end"]] for d in g.check(t)}
    assert g.load_dictionary(str(tmp_path / "missing.txt")) == 0
    with pytest.raises(ValueError):
        glance_core.Grammar(dialect="Klingon")
    assert glance_core.Grammar(dialect="British").dialect == "British"


@needs_core
def test_extension_is_safe_to_share_between_threads():
    import glance_core

    g = glance_core.Grammar()
    texts = [f"Teh cat number {k} recieve the packge." for k in range(8)]
    expected = {t: g.check(t) for t in texts}
    errors: list[str] = []

    def worker(t):
        for _ in range(5):
            if g.check(t) != expected[t]:
                errors.append(t)

    threads = [threading.Thread(target=worker, args=(t,)) for t in texts]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert errors == []


@needs_core
def test_native_client_end_to_end(tmp_path: Path):
    words = tmp_path / "dictionary.txt"
    client = NativeHarperClient(words)
    client.start()
    t = "A Zorblax here, teh end."
    assert any(t[i.start:i.end] == "Zorblax" for i in client.check(t))
    words.write_text("Zorblax\n")  # what PersonalDictionary.add writes
    assert not any(t[i.start:i.end] == "Zorblax" for i in client.check(t))
    assert any(t[i.start:i.end] == "teh" for i in client.check(t))
