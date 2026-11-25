import pytest

from glance.protocol import (
    MAX_LINE,
    SNAPSHOT,
    ApplyRequest,
    ApplyResult,
    Edit,
    FieldSnapshot,
    ProtocolError,
    Rect,
    decode,
    encode,
    text_sha256,
)


def _snap(**kw):
    base = {
        "app": "gedit", "wm_class": "gedit", "title": "t", "url": None, "role": "text",
        "text": "héllo 😀", "caret": 3, "caret_rect": Rect(1, 2, 1, 18),
        "prev_char_rect": None, "field_rect": None, "window_rect": None, "field_id": "1:a",
        "capture_confidence": 1.0, "seq": 7, "pid": 1, "gated": None,
    }
    base.update(kw)
    return FieldSnapshot(**base)


def test_snapshot_round_trips():
    s = _snap()
    t, p = decode(encode(SNAPSHOT, s.to_dict()))
    assert t == SNAPSHOT and FieldSnapshot.from_dict(p) == s


def test_snapshot_round_trips_all_rects_and_url():
    r = Rect(0, 0, 0, 0)
    s = _snap(url="https://x.test/", prev_char_rect=r, field_rect=Rect(-5, 3, 9, 9),
              window_rect=r, gated="password")
    assert FieldSnapshot.from_dict(decode(encode(SNAPSHOT, s.to_dict()))[1]) == s


def test_encode_is_single_line_even_with_newlines_in_text():
    assert encode(SNAPSHOT, {"text": "a\nb"}).count(b"\n") == 1


def test_decode_rejects_garbage_and_oversize():
    for bad in (b"not json\n", b'{"p":{}}\n', b'{"t":"x"}\n', b"x" * (MAX_LINE + 1)):
        with pytest.raises(ProtocolError):
            decode(bad)


def test_apply_messages_round_trip():
    r = ApplyRequest("a1", "f", text_sha256("hi"), Edit(0, 2, "yo"), "auto")
    assert ApplyRequest.from_dict(decode(encode("apply", r.to_dict()))[1]) == r
    res = ApplyResult("a1", "verified", "")
    assert ApplyResult.from_dict(res.to_dict()) == res


def test_text_sha256_is_stable():
    assert text_sha256("") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
