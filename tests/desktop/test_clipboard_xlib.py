"""XlibClipboard between real X clients, on a private selection.

    .venv/bin/pytest -m desktop tests/desktop/test_clipboard_xlib.py -v

Needs an X server only. The selection is a made-up atom, never CLIPBOARD; no window is
mapped and no input is sent, so the session's clipboard and the desktop are untouched.
"""
from __future__ import annotations

import os
import time

import pytest

from glance.writeback.ports import ClipSnapshot

pytestmark = pytest.mark.desktop


@pytest.fixture
def x():
    if not os.environ.get("DISPLAY"):
        pytest.skip("no X display")
    from Xlib import display

    from glance.writeback.clipboard_xlib import XlibClipboard

    selection = f"GLANCE_TEST_SEL_{os.getpid()}_{time.monotonic_ns()}"
    dpy = display.Display()
    made: list = []

    def client():
        made.append(XlibClipboard(selection))
        return made[-1]

    yield client, dpy.intern_atom
    for c in made:
        c.close()
    dpy.close()


def ask(client, target: int):
    """What `client` gets when it asks the selection owner for `target`."""
    owner = client._owner
    return owner.call(lambda: owner._convert(target, time.monotonic() + 3))


def content(atom):
    utf8, html, png = atom("UTF8_STRING"), atom("text/html"), atom("image/png")
    return {
        utf8: (utf8, 8, b"CLIP-ORIGINAL"),
        html: (html, 8, "<b>CLIP</b> \u00e9".encode()),
        png: (png, 8, os.urandom(1_000_003)),            # large enough to go by INCR
        atom("x-special/atoms"): (atom("ATOM"), 32, bytes(12)),
    }


def test_snapshot_is_byte_exact_for_every_target(x):
    client, atom = x
    app, glance = client(), client()
    original = content(atom)
    app.restore(ClipSnapshot(original))
    snap = glance.save()
    assert snap is not None and snap.payload == original


def test_replacement_goes_to_the_target_client_only(x):
    client, atom = x
    app, target, manager, glance = client(), client(), client(), client()
    original = content(atom)
    app.restore(ClipSnapshot(original))
    snap = glance.save()
    assert snap is not None

    glance.set_secret_text("the replacement \u2014 text", target._owner._win.id)
    utf8 = atom("UTF8_STRING")
    assert ask(manager, atom("TARGETS")) is None and ask(manager, utf8) is None
    got = ask(target, utf8)
    assert got is not None and got[2].decode() == "the replacement \u2014 text"
    assert ask(target, atom("x-kde-passwordManagerHint"))[2] == b"secret"

    glance.restore(snap)
    for name, value in original.items():
        assert ask(manager, name) == value
    assert ask(manager, atom("DELETE")) is None
    again = glance.save()
    assert again is not None and again.payload == original


def test_restore_does_not_overwrite_a_newer_copy(x):
    client, atom = x
    app, reader, glance = client(), client(), client()
    utf8 = atom("UTF8_STRING")
    app.restore(ClipSnapshot({utf8: (utf8, 8, b"OLD")}))
    snap = glance.save()
    glance.set_secret_text("secret", 0)
    app.restore(ClipSnapshot({utf8: (utf8, 8, b"NEWER")}))  # the user copies meanwhile
    time.sleep(0.1)
    glance.restore(snap)
    assert ask(reader, utf8)[2] == b"NEWER"


def test_oversized_content_is_not_restorable(x):
    client, atom = x
    app, glance = client(), client()
    png = atom("image/png")
    app.restore(ClipSnapshot({png: (png, 8, bytes(9 * 1024 * 1024))}))
    assert glance.save() is None


def test_empty_clipboard_is_empty_again(x):
    client, _atom = x
    glance = client()
    snap = glance.save()
    assert snap is not None and snap.payload == {}
    glance.set_secret_text("x", 0)
    glance.restore(snap)
    assert glance._owner.call(glance._owner._owner_id) == 0
