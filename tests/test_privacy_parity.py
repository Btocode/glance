"""Parity: the Rust privacy functions (`glance_core` >= 0.2) against the Python reference.

Every input goes through both implementations and must give identical results: secret spans
and kinds, protected spans, the shielded text with its mapping and segments, and unshield
(round trip, edited text, refused variants). Inputs: the generated redaction corpus (seed 7,
600 secrets + 400 benign), the unit tests' inputs, and a few thousand random texts (fixed
seed) mixing secret-shaped pieces with non-BMP, emoji, Bangla and odd whitespace.

No secret-shaped literal lives here: every such string is assembled at run time.
"""
from __future__ import annotations

import json
import os
import random
import string
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

import pytest

from glance.privacy import _native, entities, redact

REPO = Path(__file__).resolve().parents[1]
core = _native.load()

# CI sets GLANCE_REQUIRE_CORE=1 so a broken build fails here instead of skipping.
if os.environ.get("GLANCE_REQUIRE_CORE") and core is None:
    raise RuntimeError("GLANCE_REQUIRE_CORE is set but glance_core has no privacy functions")
pytestmark = pytest.mark.skipif(core is None, reason="glance_core (>= 0.2) is not built")


def rust_secrets(t):
    return [entities.Span(a, b, k) for a, b, k in core.privacy_find_secrets(t)]


def rust_protected(t):
    return [entities.Span(a, b, k) for a, b, k in core.privacy_find_protected(t)]


def rust_shield(t):
    out, mapping, objs, segs = core.privacy_shield(t)
    return redact.Shielded(out, tuple(mapping), objs, tuple(segs))


def differences(t: str) -> list[str]:
    """What differs between the two implementations on `t` (empty when identical)."""
    diffs = []
    if (py := redact.find_secrets_py(t)) != (rs := rust_secrets(t)):
        diffs.append(f"find_secrets {py} != {rs}")
    if (py := entities.find_protected_py(t)) != (rs := rust_protected(t)):
        diffs.append(f"find_protected {py} != {rs}")
    sp, sr = redact.shield_py(t), rust_shield(t)
    if sp != sr:
        diffs.append(f"shield {sp} != {sr}")
        return diffs
    edits = [sp.text, sp.text.replace("e", "E"), "x " + sp.text, sp.text[: len(sp.text) // 2],
             sp.text + sp.text, "".join(reversed(sp.text.split(" ")))]
    if sp.mapping:
        first, last = sp.mapping[0][0], sp.mapping[-1][0]
        edits += [sp.text.replace(first, ""), sp.text + first,
                  sp.text.replace(first, "\0").replace(last, first).replace("\0", last),
                  sp.text + "[[GLANCE_S99]]"]
    for e in edits:
        if (py := redact.unshield_py(e, sp)) != (rs := core.privacy_unshield(e, sp.mapping)):
            diffs.append(f"unshield({e!r}) {py!r} != {rs!r}")
    literal = "[[GLANCE_" in t  # a placeholder typed by the user fails closed instead
    if not literal and core.privacy_unshield(sp.text, sp.mapping) != t:
        diffs.append("round trip lost the text")
    return diffs


# ---- inputs --------------------------------------------------------------------------------

def corpus() -> list[str]:
    with tempfile.TemporaryDirectory(prefix="glance-corpus-") as d:
        subprocess.run([sys.executable, str(REPO / "scripts" / "gen-secret-corpus.py"),
                        "--seed", "7", "--out", d], check=True)
        secrets = [json.loads(x)["text"] for x in Path(d, "secrets.jsonl").read_text().splitlines()]
    benign_file = REPO / "tests" / "acceptance" / "corpus" / "benign.jsonl"
    benign = [json.loads(x)["text"] for x in benign_file.read_text().splitlines()]
    assert len(secrets) == 600 and len(benign) == 400
    return secrets + benign


_PK = "PRIV" + "ATE KEY"
_BEGIN, _END = "-----BEG" + "IN ", "-----EN" + "D "


def unit_inputs() -> list[str]:
    """The inputs of test_redact.py and test_entities.py, and edge cases found while porting."""
    pem = _BEGIN + "RSA " + _PK + "-----\nMIIEowIBAAKCAQEA1234\nabcdEFGH5678\n" + _END + "RSA " + _PK + "-----"
    keys = ["sk-ant-api03-" + "a1B2" * 12, "gh" + "p_" + "A1b2C3d4" * 5, "AK" + "IA" + "ABCDEFGHIJKLMNOP",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJlX2hlcmU", "4111 1111 1111 1111",
            "9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c", "xo" + "xb-123456789012-abcdefghij",
            "AIza" + "a1B2c3D4e5" * 3 + "a1B2c", "sk_" + "live_" + "a1B2c3D4e5f6g7h8",
            "glpat-" + "a1B2c3D4e5f6g7h8i9j0", "github_" + "pat_" + "A1b2C3d4E5" * 3,
            "Bearer " + "a1B2c3D4e5" * 3, "gyopbZghRddZo9FpplQteqd", "67JFo3xdjF0To39Z1p03A",
            "1tt1MnFcluLCkxTMU7ui"]
    texts = [f"use {k} for it" for k in keys] + [f"x{k}" for k in keys] + [f"é{k}é" for k in keys]
    texts += [
        "my password: hunter2 ok", f"before\n{pem}\nafter", "k\n" + _BEGIN + _PK + "-----\nMIIabc",
        'api_key = "s3cr et"', "token: 'abc'", "PASSWORD=x", "apı_key=abc", "APİ-KEY: v",
        "ſecret=1", "toKen=2", "xpassword=abc", "ক\u09be password=abc", "_token=a", "é token=b",
        "call me at 4111 1111 1111 1112", "order 12345678 shipped",
        "internationalization-and-localization", "https://example.test/a/b", "commit abc1234def",
        "at " + "a1b2c3d4e5" * 4, "hey @sam use sk-ant-api03-" + "a1B2" * 12 + " see https://x.test/p",
        "see https://x.test/?k=" + "sk-" + "a1B2" * 8 + " ok", "ping @a then @b", "teh @sam teh",
        "hi ￼ there", "on 2024-05-01 4111 1111 1111 1111", "Room 12 4111111111111111 ok",
        "order 12 4111111111111111", "4111111111111111", "keep [[GLANCE_E1]] and @sam",
        "literal [[GLANCE_E1]] only", "pay 4111\n1111 1111\t1111 now",
        "Open /var/notes/budget_2024.xlsx first", "see /usr/lib/python3.12/site-packages",
        "call parseHtml5DocumentNode() here", "the COVID19VaccinationCenter page",
        "on a SamsungGalaxyS24Ultra5G phone", "@sam.lee ￼ ￼ ￼ Teh",
        "see http://x.test and www.y.test/z ok", "ping <@U123ABC> and <@!42> in #dev-ops now",
        "(@a-b.c)", "C# and a#b are fine", "thumbs :+1: and :smile_cat:", "a ```\nx = `1`\n``` b",
        "x ```py\nnever closed `a`", "a `b` c `d e`", "lone ` backtick", "a\u200db \ufe0f",
        "go https://x.test/@sam/#frag:a1: now", "run `curl https://x.test @me` ok",
        "hi @sam see https://x.test/a?b=1 :tada: `rm -rf` ￼ ok \U0001F600",
        "`a` https://x.test/`b` @c:d: ```e``` #f ￼\U0001F600:g:", "@a@b #c#d :e::f: ``` ` ``` `",
        "\U0001F600\U0001F600 x @sam", "ok ❤\ufe0f ok", "ok \U0001F44D\U0001F3FD ok",
        "ok \U0001F468\u200d\U0001F469\u200d\U0001F467 ok", "\U0001F1E7\U0001F1E9", "☃ ok", "\U0001F600\u200d",
        "আম\u09bf @রহ\u09bfম ক\u09c7 #ব\u09be\u0982ল\u09be ল\u09bfখ\u09bf ৪১১১ ১১১১ ১১১১ ১১১১", "৪১১১১১১১১১১১১১১১", "١٢٣٤٥٦٧٨٩٠١٢٣٤",
        "Bearer\u3000" + "a1B2c3D4e5" * 3, "Bearer\x1c" + "a1B2c3D4e5" * 3, "@é#ü", "a@b",
        "word\u0301@x", "x\u09be@y", "Ⅻ@z", "²#q", "", "1 " * 500,
    ]
    return texts


PIECES = (
    "password", "Passwd", "pwd", "secret", "TOKEN", "api_key", "api-key", "apikey", "apı_key",
    "ſecret", "=", ":", " = ", ": ", '"', "'", "Bearer ", "eyJ", ".", "sk-", "gh" + "p_", "AK" + "IA",
    "xo" + "xb-", "AIza", "sk_" + "test_", "glpat-", "github_" + "pat_", _BEGIN + "RSA " + _PK + "-----",
    _END + "RSA " + _PK + "-----", "\n", "\t", "\r\n", " ", "  ", "\x1c", "\x85", "\xa0", "\u2028",
    "\u3000", "\u200b", "```", "`", "https://", "http://", "www.", "@", "#", "<@", ">", ":smile:",
    ":+1:", "￼", "\U0001F600", "\U0001F44D\U0001F3FD", "\u200d", "\ufe0f", "⃣", "\U0001F1E7\U0001F1E9", "☃",
    "\U000E0067", "আম\u09bf", "ব\u09be\u0982ল\u09be", "ক\u09be", "\u09be", "\u09cd", "০১২৩৪৫৬৭৮৯", "٣", "𝟘𝟙", "Ⅻ", "²", "é",
    "e\u0301", "İ", "ı", "K", "ß", "Ω", "中文", "𝒜", "/", "+", "-", "_", "4111", "1111", "-", "--",
)
B62 = string.ascii_letters + string.digits


def random_text(rng: random.Random) -> str:
    parts = []
    for _ in range(rng.randint(1, 14)):
        r = rng.random()
        if r < 0.45:
            parts.append(rng.choice(PIECES))
        elif r < 0.6:
            alpha = rng.choice((B62, B62 + "+/=", B62 + "-_", "0123456789abcdef", string.ascii_lowercase))
            parts.append("".join(rng.choice(alpha) for _ in range(rng.randint(8, 48))))
        elif r < 0.75:
            sep = rng.choice(("", " ", "-", "\n", "\t", "  "))
            parts.append(sep.join("".join(rng.choice(string.digits) for _ in range(rng.randint(1, 6)))
                                  for _ in range(rng.randint(1, 6))))
        elif r < 0.9:
            parts.append(rng.choice(("the", "meeting", "Hello", "world", "x", "and", ",", ".")))
        else:
            parts.append("".join(chr(rng.choice((rng.randint(32, 0x2FF), rng.randint(0x980, 0x9FF),
                                                  rng.randint(0x1F300, 0x1FAFF), rng.randint(0x2000, 0x2BFF))))
                                 for _ in range(rng.randint(1, 4))))
        if rng.random() < 0.5:
            parts.append(" ")
    return "".join(parts)


def any_code_point_text(rng: random.Random) -> str:
    """Code points from the whole range (no surrogates): only meaningful when this Python's
    Unicode version is the one the Rust tables were generated from."""
    out = []
    for _ in range(rng.randint(1, 40)):
        cp = rng.choice((rng.randint(0, 0x7F), rng.randint(0x80, 0xFFFF), rng.randint(0x10000, 0x10FFFF)))
        if not 0xD800 <= cp <= 0xDFFF:
            out.append(chr(cp))
        if rng.random() < 0.2:
            out.append(rng.choice(PIECES))
    return "".join(out)


# ---- tests ---------------------------------------------------------------------------------

def _report(texts: list[str]) -> list[str]:
    bad = []
    for t in texts:
        if d := differences(t):
            bad.append(f"{t!r}: {d[0]}")
    return bad


def test_corpus_parity():
    bad = _report(corpus())
    assert not bad, f"{len(bad)} of 1000 corpus texts differ:\n" + "\n".join(bad[:10])


def test_unit_input_parity():
    texts = unit_inputs()
    bad = _report(texts)
    assert not bad, f"{len(bad)} of {len(texts)} differ:\n" + "\n".join(bad[:10])


def test_random_text_parity():
    rng = random.Random(20261126)
    texts = [random_text(rng) for _ in range(4000)]
    bad = _report(texts)
    assert not bad, f"{len(bad)} of 4000 random texts differ:\n" + "\n".join(bad[:10])


@pytest.mark.skipif(unicodedata.unidata_version != getattr(core, "PRIVACY_UNICODE_VERSION", None),
                    reason="this Python's Unicode differs from the one the Rust tables carry")
def test_whole_code_point_range_parity():
    rng = random.Random(7)
    texts = [any_code_point_text(rng) for _ in range(2000)]
    bad = _report(texts)
    assert not bad, f"{len(bad)} of 2000 texts differ:\n" + "\n".join(bad[:10])


def test_lone_surrogate_falls_back_to_python():
    t = "use \ud800 " + "sk-" + "a1B2" * 8 + " @sam"
    with pytest.raises(UnicodeEncodeError):
        core.privacy_find_secrets(t)
    assert redact.find_secrets(t) == redact.find_secrets_py(t)
    assert entities.find_protected(t) == entities.find_protected_py(t)
    s = redact.shield(t)
    assert s == redact.shield_py(t) and redact.unshield(s.text, s) == t


@pytest.mark.skipif(bool(os.environ.get(_native.PURE_ENV)), reason="GLANCE_PURE_PYTHON is set")
def test_public_functions_use_the_extension():
    assert _native.core is core
    assert _native.load({"GLANCE_PURE_PYTHON": "1"}) is None
    assert _native.load({"GLANCE_PURE_PYTHON": "0"}) is core
