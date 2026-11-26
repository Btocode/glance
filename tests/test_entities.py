from itertools import pairwise

import pytest

from glance.privacy.entities import Span, find_protected, overlaps

pytestmark = pytest.mark.usefixtures("privacy_impl")  # Rust and Python


def found(t: str) -> list[tuple[str, str]]:
    return [(t[s.start:s.end], s.kind) for s in find_protected(t)]


def test_find_protected_kinds():
    t = "hi @sam see https://x.test/a?b=1 :tada: `rm -rf` ￼ ok \U0001F600"
    assert [(t[s.start:s.end], s.kind) for s in find_protected(t)] == [
        ("@sam", "mention"), ("https://x.test/a?b=1", "url"), (":tada:", "emoji"),
        ("`rm -rf`", "code"), ("￼", "object"), ("\U0001F600", "emoji")]


def test_email_address_is_not_a_mention():
    assert find_protected("mail me at a@b.test") == []


def test_plain_text_has_no_spans():
    assert find_protected("") == []
    assert find_protected("Teh quick brown fox, isn't it? 3 * 4 = 12.") == []


def test_each_embedded_object_is_its_own_span():
    assert find_protected("a￼￼b") == [Span(1, 2, "object"), Span(2, 3, "object")]


def test_slack_draft_from_findings():
    # S2.5: mention is literal text; emoji, link and code are one U+FFFC each.
    assert found("@sam.lee ￼ ￼ ￼ Teh") == [
        ("@sam.lee", "mention"), ("￼", "object"), ("￼", "object"),
        ("￼", "object")]


@pytest.mark.parametrize("text, expected", [
    ("see http://x.test and www.y.test/z ok",
     [("http://x.test", "url"), ("www.y.test/z", "url")]),
    ("ping <@U123ABC> and <@!42> in #dev-ops now",
     [("<@U123ABC>", "mention"), ("<@!42>", "mention"), ("#dev-ops", "mention")]),
    ("(@a-b.c)", [("@a-b.c", "mention")]),
    ("C# and a#b are fine", []),
    ("thumbs :+1: and :smile_cat:", [(":+1:", "emoji"), (":smile_cat:", "emoji")]),
    ("a ```\nx = `1`\n``` b", [("```\nx = `1`\n```", "code")]),
    ("x ```py\nnever closed `a`", [("```py\nnever closed `a`", "code")]),
    ("a `b` c `d e`", [("`b`", "code"), ("`d e`", "code")]),
    ("lone ` backtick", []),
])
def test_patterns(text, expected):
    assert found(text) == expected


@pytest.mark.parametrize("emoji", [
    "\U0001F600",                                  # grinning face
    "❤️",                                # red heart + VS16
    "\U0001F44D\U0001F3FD",                        # thumbs up + skin tone
    "\U0001F468‍\U0001F469‍\U0001F467",  # ZWJ family
    "\U0001F1E7\U0001F1E9",                        # flag (regional indicators)
    "☃",                                      # snowman (category So)
])
def test_unicode_emoji_sequence_is_one_span(emoji):
    t = f"ok {emoji} ok"
    assert found(t) == [(emoji, "emoji")]


def test_joiner_without_emoji_is_not_protected():
    assert find_protected("a‍b ️") == []


def test_overlapping_matches_merge_into_one_span():
    t = "go https://x.test/@sam/#frag:a1: now"
    assert found(t) == [("https://x.test/@sam/#frag:a1:", "url")]
    t = "run `curl https://x.test @me` ok"
    assert found(t) == [("`curl https://x.test @me`", "code")]


@pytest.mark.parametrize("text", [
    "hi @sam see https://x.test/a?b=1 :tada: `rm -rf` ￼ ok \U0001F600",
    "`a` https://x.test/`b` @c:d: ```e``` #f ￼\U0001F600:g:",
    "@a@b #c#d :e::f: ``` ` ``` `",
])
def test_spans_sorted_and_non_overlapping(text):
    spans = find_protected(text)
    assert all(0 <= s.start < s.end <= len(text) for s in spans)
    assert all(a.end <= b.start for a, b in pairwise(spans))


def test_offsets_are_code_points_after_astral_chars():
    t = "\U0001F600\U0001F600 x @sam"
    assert find_protected(t)[-1] == Span(5, 9, "mention")


def test_overlaps():
    spans = [Span(3, 7, "mention"), Span(12, 13, "object")]
    assert overlaps(spans, 3, 7) and overlaps(spans, 0, 4) and overlaps(spans, 6, 20)
    assert overlaps(spans, 4, 5) and overlaps(spans, 12, 13)
    assert not overlaps(spans, 0, 3) and not overlaps(spans, 7, 12)
    assert not overlaps(spans, 13, 20) and not overlaps([], 0, 100)


def test_overlaps_zero_width_edit():
    spans = [Span(3, 7, "mention")]
    assert overlaps(spans, 5, 5)                                   # insertion inside
    assert not overlaps(spans, 3, 3) and not overlaps(spans, 7, 7)  # insertion at an edge
