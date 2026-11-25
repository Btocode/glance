import pytest

from glance.capture.ports import FILLER, clean_text, is_password_input, to_clean, to_raw

EMOJI = "\U0001f600"
GECKO = f"ab{EMOJI}{FILLER}cd"  # findings S3.1: 61 62 1f600 feff 63 64
SLATE = f"{FILLER}\n"  # findings S2.1: an empty Slate line


def test_clean_text_drops_fillers_only():
    assert clean_text(GECKO) == f"ab{EMOJI}cd"
    assert clean_text(SLATE) == "\n"
    assert clean_text("plain ￼ text") == "plain ￼ text"


@pytest.mark.parametrize("raw_offset, clean", [(0, 0), (2, 2), (3, 3), (4, 3), (5, 4), (6, 5)])
def test_to_clean_gecko(raw_offset, clean):
    assert to_clean(GECKO, raw_offset) == clean


@pytest.mark.parametrize("clean, raw_offset", [(0, 0), (2, 2), (3, 4), (4, 5), (5, 6), (9, 6)])
def test_to_raw_gecko(clean, raw_offset):
    assert to_raw(GECKO, clean) == raw_offset


def test_range_over_astral_char_takes_its_filler():
    start, end = to_raw(GECKO, 2), to_raw(GECKO, 3)
    assert GECKO[start:end] == EMOJI + FILLER


def test_round_trip_at_every_caret_position():
    for raw in (GECKO, SLATE, "plain", "", f"{EMOJI}{FILLER}{EMOJI}{FILLER}"):
        clean = clean_text(raw)
        for i in range(len(clean) + 1):
            assert to_clean(raw, to_raw(raw, i)) == i


def test_slate_empty_line_caret():
    assert to_clean(SLATE, 1) == 0 and to_raw(SLATE, 0) == 1


def test_offsets_are_clamped():
    assert to_clean("abc", -1) == 0 and to_clean("abc", 99) == 3
    assert to_raw("abc", -5) == 0 and to_raw("abc", 99) == 3


def test_without_fillers_offsets_are_identity():
    raw = f"a{EMOJI}b"  # Chromium: code-point offsets, no filler
    assert [to_raw(raw, i) for i in range(4)] == [0, 1, 2, 3]
    assert [to_clean(raw, i) for i in range(4)] == [0, 1, 2, 3]


@pytest.mark.parametrize("attrs, expected", [
    ({"text-input-type": "password", "tag": "input"}, True),
    ({"text-input-type": "Password"}, True),
    ({"text-input-type": "text"}, False),
    ({"tag": "input"}, False),
    ({}, False),
    (None, False),
])
def test_is_password_input(attrs, expected):
    assert is_password_input(attrs) is expected
