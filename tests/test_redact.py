import pytest

from glance.privacy.redact import can_apply_rewrite, find_secrets, shield, unshield

pytestmark = pytest.mark.usefixtures("privacy_impl")  # Rust and Python

_PK = "PRIV" + "ATE KEY"
PEM = "-----BEG" + "IN RSA " + _PK + "-----\nMIIEowIBAAKCAQEA1234\nabcdEFGH5678\n-----END RSA " + _PK + "-----"


def spans_text(t):
    return [t[s.start:s.end] for s in find_secrets(t)]


@pytest.mark.parametrize("secret, kind", [
    ("sk-ant-api03-" + "a1B2" * 12, "api_key"), ("gh" + "p_" + "A1b2C3d4" * 5, "api_key"),
    ("AK" + "IA" + "ABCDEFGHIJKLMNOP", "api_key"),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJlX2hlcmU", "jwt"),
    ("4111 1111 1111 1111", "card"), ("9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c", "entropy")])
def test_secret_kinds(secret, kind):
    t = f"use {secret} for it"
    s = find_secrets(t)
    assert [(t[x.start:x.end], x.kind) for x in s] == [(secret, kind)]


def test_assignment_and_pem():
    t = "my password: hunter2 ok"
    assert "hunter2" in spans_text(t) and "password" not in spans_text(t)
    t = f"before\n{PEM}\nafter"
    s = find_secrets(t)
    assert [(t[x.start:x.end], x.kind) for x in s] == [(PEM, "pem")]


def test_not_secrets():
    for t in ("call me at 4111 1111 1111 1112", "order 12345678 shipped",
              "internationalization-and-localization", "https://example.test/a/b",
              "commit abc1234def"):
        assert find_secrets(t) == []


def test_git_sha_may_be_redacted():
    assert len(find_secrets("at " + "a1b2c3d4e5" * 4)) == 1


def test_shield_round_trip():
    t = "hey @sam use sk-ant-api03-" + "a1B2" * 12 + " see https://x.test/p"
    s = shield(t)
    assert "sk-ant" not in s.text and "@sam" not in s.text and s.text.count("[[GLANCE_") == 3
    assert unshield(s.text, s) == t
    assert unshield(s.text.replace("hey", "Hello"), s) == t.replace("hey", "Hello")


def test_secret_inside_entity_is_secret():
    t = "see https://x.test/?k=" + "sk-" + "a1B2" * 8 + " ok"
    s = shield(t)
    assert "[[GLANCE_S1]]" in s.text and "a1B2" not in s.text
    assert unshield(s.text, s) == t


def test_unshield_refuses_dropped_duplicated_reordered_or_invented():
    s = shield("ping @a then @b")
    p1, p2 = "[[GLANCE_E1]]", "[[GLANCE_E2]]"
    assert unshield(f"ping {p1} then {p2}", s) == "ping @a then @b"
    for bad in (f"ping {p1}", f"ping {p1} {p1} {p2}", f"ping {p2} then {p1}",
                f"ping {p1} {p2} [[GLANCE_E9]]"):
        assert unshield(bad, s) is None


def test_map_span():
    s = shield("teh @sam teh")
    i = s.text.rindex("teh")
    assert s.map_span(i, i + 3) == (9, 12) and s.map_span(4, 6) is None
    assert s.map_span(0, 3) == (0, 3)


def test_objects_block_apply():
    assert not can_apply_rewrite(shield("hi ￼ there")) and can_apply_rewrite(shield("hi there"))


@pytest.mark.parametrize("secret", [
    "xo" + "xb-123456789012-abcdefghij", "AIza" + "a1B2c3D4e5" * 3 + "a1B2c", "sk_" + "live_" + "a1B2c3D4e5f6g7h8",
    "glpat-" + "a1B2c3D4e5f6g7h8i9j0", "github_" + "pat_" + "A1b2C3d4E5" * 3, "Bearer " + "a1B2c3D4e5" * 3,
])
def test_more_api_keys(secret):
    t = f"x {secret} y"
    assert [(t[x.start:x.end], x.kind) for x in find_secrets(t)] == [(secret, "api_key")]


def test_unterminated_pem_and_quoted_assignment():
    t = "k\n-----BEG" + "IN " + _PK + "-----\nMIIabc"
    assert [x.kind for x in find_secrets(t)] == ["pem"]
    assert spans_text('api_key = "s3cr et"') == ["s3cr et"]
    assert spans_text("token: 'abc'") == ["abc"]


@pytest.mark.parametrize("t, card", [
    ("on 2024-05-01 4111 1111 1111 1111", "4111 1111 1111 1111"),
    ("Room 12 4111111111111111 ok", "4111111111111111"),
    ("order 12 4111111111111111", "4111111111111111"),
    ("4111111111111111", "4111111111111111"),
])
def test_card_with_leading_digits(t, card):
    found = find_secrets(t)
    assert len(found) == 1 and card in t[found[0].start:found[0].end]
    assert t[found[0].end:].strip() in ("", "ok")


def test_literal_placeholder_in_user_text_fails_closed():
    t = "keep [[GLANCE_E1]] and @sam"
    s = shield(t)
    assert unshield(s.text, s) is None
    assert unshield("no secrets here", shield("no secrets here")) == "no secrets here"
    s2 = shield("literal [[GLANCE_E1]] only")
    assert unshield(s2.text, s2) is None


def test_card_split_across_whitespace():
    t = "pay 4111\n1111 1111\t1111 now"
    found = find_secrets(t)
    assert [(t[x.start:x.end], x.kind) for x in found] == [("4111\n1111 1111\t1111", "card")]


def test_card_detector_agrees_with_luhn_on_samples():
    import random

    from glance.privacy.redact import _luhn
    rnd = random.Random(7)
    for _ in range(300):
        d = "".join(rnd.choice("0123456789") for _ in range(rnd.randint(13, 19)))
        if _luhn(d):
            assert [(x.start, x.end) for x in find_secrets(d)] == [(0, len(d))]


@pytest.mark.parametrize("make", [
    lambda r: "1 " * 10000,
    lambda r: "".join(r.choice("0123456789  ") for _ in range(20000)),
])
def test_find_secrets_is_fast(make):
    import random
    import time
    t = make(random.Random(1))
    best = float("inf")
    for _ in range(3):  # best of three: a parallel test run can stall one attempt
        t0 = time.perf_counter()
        find_secrets(t)
        best = min(best, time.perf_counter() - t0)
    assert best < 0.05


@pytest.mark.parametrize("token", [
    "gyopbZghRddZo9FpplQteqd", "67JFo3xdjF0To39Z1p03A", "1tt1MnFcluLCkxTMU7ui",
])
def test_short_random_tokens_below_4_bits_are_secrets(token):
    # 20-23 random characters repeat some: 3.85-3.92 bits, under the 4.0 for longer tokens.
    assert [x.kind for x in find_secrets(f"use {token} now")] == ["entropy"]


@pytest.mark.parametrize("text", [
    "Open /var/notes/budget_2024.xlsx first",
    "Open /lib/lib/projects/home/budget_2024.xlsx and check",
    "see /usr/lib/python3.12/site-packages",
    "call parseHtml5DocumentNode() here",
    "the COVID19VaccinationCenter page",
    "on a SamsungGalaxyS24Ultra5G phone",
])
def test_paths_and_identifiers_are_not_secrets(text):
    assert find_secrets(text) == []
