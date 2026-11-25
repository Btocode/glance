import os
import statistics
import time

import pytest

from glance.grammar.harper import HarperClient
from glance.paths import harper_bin


@pytest.fixture
def client(tmp_path, monkeypatch):
    binary = harper_bin()
    if not binary.exists():
        pytest.skip("harper-ls not installed (run scripts/fetch-harper.sh)")
    # keep harper-ls from writing into the real ~/.config/harper-ls
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    c = HarperClient(binary, tmp_path / "dictionary.txt", timeout_s=2.0)
    c.start()
    yield c
    c.stop()


def test_live_check_finds_misspelling(client):
    issues = client.check("Teh quick brown fox.")
    teh = [i for i in issues if (i.start, i.end) == (0, 3)]
    assert teh and "The" in teh[0].replacements and teh[0].rule == "SpellCheck"
    assert not client.degraded


def test_live_astral_prefix_maps_to_codepoints(client):
    t = "😀 Thsi is fine."
    issues = client.check(t)
    assert any(t[i.start:i.end] == "Thsi" for i in issues)


def test_live_check_under_150ms(client):
    text = ("Teh quick brown fox jump over the lazy dog, i think its fine. " * 9)[:500]
    client.check(text)
    times = []
    for k in range(20):
        t0 = time.perf_counter()
        client.check(text + " " * (k % 2))
        times.append(time.perf_counter() - t0)
    assert statistics.median(times) < 0.15


def test_live_disabled_rule_is_not_reported(client):
    t = "i think so"
    assert any(i.rule == "CapitalizePersonalPronouns" for i in client.check(t))
    client.set_disabled_rules({"CapitalizePersonalPronouns"})
    assert not any(i.rule == "CapitalizePersonalPronouns" for i in client.check(t))
    client.set_disabled_rules(set())
    assert any(i.rule == "CapitalizePersonalPronouns" for i in client.check(t))


def test_live_dictionary_reload(client, tmp_path):
    t = "A Zorblax here."
    assert any(t[i.start:i.end] == "Zorblax" for i in client.check(t))
    (tmp_path / "dictionary.txt").write_text("Zorblax\n")
    client.reload_dictionary()
    assert client.alive
    assert not any(t[i.start:i.end] == "Zorblax" for i in client.check(t))


def test_killed_harper_reports_not_alive(client):
    client.check("fine")
    client._proc.kill()
    client._proc.wait()
    assert client.check("Teh cat") == []
    assert client.alive is False
    assert os.path.exists(client._user_dict.parent)


def test_live_first_check_after_start_meets_the_production_timeout(tmp_path, monkeypatch):
    # The first word far from every dictionary word costs harper-ls ~1 s once (a lazy
    # structure is built). Under the daemon's 0.5 s budget that check gave up and returned
    # nothing, and nothing checked again until the text changed: a preset or freshly typed
    # text with such a word showed no issue at all. A slow lint now gets SLOW_LINT_S more.
    binary = harper_bin()
    if not binary.exists():
        pytest.skip("harper-ls not installed (run scripts/fetch-harper.sh)")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    c = HarperClient(binary, tmp_path / "dictionary.txt")  # default timeout, as in glanced
    c.start()
    try:
        issues = c.check("Teh cat saw a zqxjword.")
        assert {(i.start, i.end, i.rule) for i in issues} >= {
            (0, 3, "SpellCheck"), (14, 22, "SpellCheck")}
        assert not c.degraded
    finally:
        c.stop()
