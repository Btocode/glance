"""Parity: the in-process engine (`glance_core`) against harper-ls 3.3.0, Issue for Issue.

Runs every text of tests/fixtures/harper/parity_corpus.txt (plus the user-report fixture)
through both engines and compares the resulting Issues (span, rule, message, replacements,
safe). Needs the extension (`maturin develop` in rust/glance_core) and a harper-ls binary:
$GLANCE_HARPER_LS, else ~/.local/share/glance/bin/harper-ls (only run, never modified; it
runs with HOME and the XDG dirs pointed at a temp dir so it cannot read or write the user's
own harper-ls files).

One known, accepted difference (KNOWN_LS_BUG): harper-ls answers a code-action request by
reading the range's UTF-16 column as a character index, so on a line with a non-BMP
character (an emoji) before a lint it returns the suggestions of a spot further right.
The native engine has no such conversion; there the spans and rules still must match, and the
replacements must be the native lint's own suggestions.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from glance.grammar.harper import HarperClient, diagnostics_to_issues
from glance.grammar.harper_native import NativeHarperClient, available
from glance.paths import harper_bin

FIXTURES = Path(__file__).parent / "fixtures" / "harper"
DICT_WORDS = ("kubectl", "Neovim")

# CI sets GLANCE_REQUIRE_CORE=1 so a broken build fails here instead of skipping.
if os.environ.get("GLANCE_REQUIRE_CORE") and not available():
    raise RuntimeError("GLANCE_REQUIRE_CORE is set but glance_core does not import")
pytestmark = pytest.mark.skipif(not available(), reason="glance_core is not built")


def corpus() -> list[str]:
    out = []
    for line in (FIXTURES / "parity_corpus.txt").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            out.append(line.replace("\\n", "\n"))
    return out


def _harper_ls() -> Path | None:
    env = os.environ.get("GLANCE_HARPER_LS")
    path = Path(env) if env else harper_bin()
    return path if path.is_file() and os.access(path, os.X_OK) else None


def astral_before_on_line(text: str, pos: int) -> bool:
    line_start = text.rfind("\n", 0, pos) + 1
    return any(ord(c) > 0xFFFF for c in text[line_start:pos])


@pytest.fixture(scope="module")
def engines(tmp_path_factory):
    binary = _harper_ls()
    if binary is None and os.environ.get("GLANCE_REQUIRE_CORE"):
        pytest.fail("GLANCE_REQUIRE_CORE is set but there is no harper-ls to compare with")
    if binary is None:
        pytest.skip("no harper-ls (set GLANCE_HARPER_LS or run scripts/fetch-harper.sh)")
    home = tmp_path_factory.mktemp("parity-home")
    words = home / "dictionary.txt"
    words.write_text("".join(w + "\n" for w in DICT_WORDS), encoding="utf-8")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOME", str(home))
        for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
            mp.setenv(var, str(home / var.lower()))
        ls = HarperClient(binary, words, timeout_s=5.0)
        ls.start()
    native = NativeHarperClient(words)
    native.start()
    yield ls, native
    ls.stop()


def _key(i):
    return (i.start, i.end, i.rule, i.message, i.replacements, i.safe)


def compare(text, ls_issues, native_issues):
    """Differences that are not KNOWN_LS_BUG, as strings (empty: identical)."""
    ls_by = {(i.start, i.end, i.rule): i for i in ls_issues}
    nat_by = {(i.start, i.end, i.rule): i for i in native_issues}
    problems = []
    for k in sorted(set(ls_by) | set(nat_by)):
        a, b = ls_by.get(k), nat_by.get(k)
        if a is None or b is None:
            problems.append(f"only {'native' if a is None else 'harper-ls'}: {k}")
        elif _key(a) != _key(b):
            if astral_before_on_line(text, k[0]) and a.message == b.message:
                continue  # KNOWN_LS_BUG: harper-ls's suggestions come from the wrong spot
            problems.append(f"{k}: harper-ls {_key(a)} != native {_key(b)}")
    return problems


def test_corpus_matches_harper_ls(engines):
    ls, native = engines
    texts = corpus()
    assert len(texts) >= 70
    report, identical, known = [], 0, 0
    for text in texts:
        a, b = ls.check(text), native.check(text)
        assert not ls.degraded and not native.degraded, text
        problems = compare(text, a, b)
        if problems:
            report.append(f"{text!r}:\n  " + "\n  ".join(problems))
        elif [_key(i) for i in a] == [_key(i) for i in b]:
            identical += 1
        else:
            known += 1
    print(f"\nparity: {len(texts)} texts, {identical} identical, {known} known harper-ls "
          f"code-action differences, {len(report)} unexplained")
    assert not report, "\n".join(report)


def test_known_ls_bug_only_moves_replacements(engines):
    ls, native = engines
    t = "😀 first\n😀 secnd line with emoji\nthird line normal teh"
    a, b = ls.check(t), native.check(t)
    assert [(i.start, i.end, i.rule) for i in a] == [(i.start, i.end, i.rule) for i in b]
    secnd = next(i for i in b if t[i.start:i.end] == "secnd")
    assert "second" in secnd.replacements


def test_user_report_fixture_matches_the_recorded_harper_ls_output(engines):
    _, native = engines
    fx = json.loads((FIXTURES / "user_report_ai_sentence.json").read_text(encoding="utf-8"))
    actions = {int(k): v for k, v in fx["actions"].items()}
    recorded = diagnostics_to_issues(fx["text"], fx["diagnostics"], actions)
    assert [_key(i) for i in native.check(fx["text"])] == [_key(i) for i in recorded]


def test_personal_dictionary_reaches_both_engines(engines):
    ls, native = engines
    t = "Then I opened Neovim and ran kubectl."
    for client in engines:
        flagged = {t[i.start:i.end] for i in client.check(t)}
        assert not flagged & set(DICT_WORDS), (type(client).__name__, flagged)
    del ls, native
