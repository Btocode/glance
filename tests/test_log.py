import pytest

from glance.log import UnsafeLogValue, fingerprint, log_event


def test_log_event_redacts_free_text(capsys, monkeypatch):
    monkeypatch.delenv("GLANCE_STRICT_LOG")
    log_event("x", n=3, wm="slack", t="hello there friend")
    err = capsys.readouterr().err
    assert "t=<redacted>" in err and "hello" not in err
    assert "n=3" in err and "wm=slack" in err and err.startswith("x ")


def test_log_event_strict_raises():
    with pytest.raises(UnsafeLogValue):
        log_event("x", t="two words")


def test_log_event_allows_scalars(capsys):
    log_event("x", a=1, b=2.5, c=True, d=None)
    err = capsys.readouterr().err
    assert "a=1" in err and "b=2.5" in err and "c=True" in err and "d=None" in err


def test_log_event_rejects_long_string(capsys, monkeypatch):
    monkeypatch.delenv("GLANCE_STRICT_LOG")
    log_event("x", s="a" * 65)
    assert "s=<redacted>" in capsys.readouterr().err


def test_fingerprint_has_no_text():
    f = fingerprint("secret words")
    assert f.startswith("len=12,sha=") and "secret" not in f
    assert len(f.split("sha=")[1]) == 8
