"""Capture matrix (spec §8.1): 200 scripted edits per (browser, fixture page) and for the GTK
app; Glance's captured text and caret must equal the editor's own, 99 % of the time.

    .venv/bin/pytest -m apps tests/acceptance/test_capture_matrix.py -v

Everything runs on a private Xvfb with its own D-Bus and AT-SPI bus and throwaway browser
profiles; nothing touches the desktop in use.
"""
from __future__ import annotations

import itertools
import os
import shutil

import pytest

from tests.acceptance.harness import (
    BROWSERS,
    Browser,
    BrowserPage,
    GlanceUnderTest,
    GtkApp,
    Typist,
    norm,
    run_capture_matrix,
    settle,
)

pytestmark = pytest.mark.apps

PAGES = ("textarea", "contenteditable", "prosemirror", "slate", "lexical", "quill")
# The Firefox snap cannot run inside the private session: `snap run` needs the real systemd
# user bus to create its scope and dies with "not a snap cgroup for tag snap.firefox.firefox"
# on a private bus, and pointing it at the real bus would mix it into the user's session.
# Set GLANCE_FIREFOX_BIN to a non-snap Firefox to run these pairs.
NO_GO: dict[str, str] = {} if os.environ.get("GLANCE_FIREFOX_BIN") else {
    "firefox": "the Firefox snap does not start on a private D-Bus (snap-confine: 'not a "
               "snap cgroup'); set GLANCE_FIREFOX_BIN to a non-snap Firefox"}


def _check(r) -> None:
    assert r.exact / r.total >= 0.99, (r.exact, r.total, r.mismatches[:5])
    # The edits really happened, judged by the editor's own text.
    assert len(r.final_text) > 100, r.final_text
    assert r.type_changed / r.type_ops >= 0.95, (r.type_changed, r.type_ops)
    assert r.paste_ops and r.paste_landed == r.paste_ops, (r.paste_landed, r.paste_ops)


def _record(name: str, r) -> None:
    """Print the score (pytest -rP) and append it to $GLANCE_MATRIX_LOG when that is set."""
    line = f"{name}\t{r.exact}/{r.total}\t{r.mismatches[:3]}"
    print(line)
    if os.environ.get("GLANCE_MATRIX_LOG"):
        with open(os.environ["GLANCE_MATRIX_LOG"], "a") as f:
            f.write(line + "\n")


def _need(kind: str) -> None:
    if kind in NO_GO:
        pytest.skip(NO_GO[kind])
    if not os.access(BROWSERS[kind], os.X_OK) and shutil.which(kind) is None:
        pytest.skip(f"{kind} is not installed")


@pytest.fixture
def typist(session):
    t = Typist(session)
    yield t
    t.close()


@pytest.fixture
def glance(session, tmp_path):
    g = GlanceUnderTest(session, tmp_path).start()
    yield g
    g.stop()


def _open(kind, page, session, typist, fixture_server, profile_root, query=""):
    server, base = fixture_server
    browser = Browser(kind, profile_root, session, server)
    target = BrowserPage(session, typist, server, browser, f"{base}/{page}.html{query}")
    target.start()
    return target


@pytest.mark.parametrize("page", PAGES)
@pytest.mark.parametrize("kind", ["chrome", "brave", "firefox"])
def test_capture_matches_the_editor(kind, page, session, typist, glance, fixture_server,
                                    profile_root):
    _need(kind)
    target = _open(kind, page, session, typist, fixture_server, profile_root)
    try:
        glance.last_snapshot(30.0)
        r = run_capture_matrix(target, glance)
        _record(f"{kind}/{page}", r)
        _check(r)
    finally:
        target.stop()


def test_capture_matches_the_gtk_app(session, typist, glance):
    target = GtkApp(session, typist)
    target.start()
    try:
        glance.last_snapshot(20.0)
        r = run_capture_matrix(target, glance)
        _record("gtk/textview", r)
        _check(r)
    finally:
        target.stop()


def test_mention_atom_reported_as_object(session, typist, glance, fixture_server,
                                         profile_root):
    target = _open("chrome", "prosemirror", session, typist, fixture_server, profile_root,
                   "?mention")
    try:
        glance.last_snapshot(30.0)
        # The first snapshot, taken before any caret event, is the editor root's one block.
        # Type a key and the tracker reads the paragraph the caret is in.
        typist.type("x")
        settle(target, glance)
        snap = glance.snapshot_now()
        assert "\ufffc" in snap.text, repr(snap.text)
        assert norm(snap.text) == norm(target.truth()["text"])
    finally:
        target.stop()


def test_caret_rect_tracks_caret(session, typist, glance, fixture_server, profile_root):
    target = _open("chrome", "textarea", session, typist, fixture_server, profile_root)
    try:
        glance.last_snapshot(30.0)
        xs = []
        for ch in "abcdefghij":
            typist.type(ch)
            settle(target, glance)
            snap = glance.snapshot_now()
            # At the end of the text Chromium reports no caret rectangle (spike S2.1): the
            # caret is then at the right edge of the previous character.
            r = snap.caret_rect or snap.prev_char_rect
            assert r is not None, snap.caret
            xs.append(r.x if snap.caret_rect else r.x + r.w)
        assert all(a < b for a, b in itertools.pairwise(xs)), xs
        f = snap.field_rect
        assert f.x <= xs[0] and xs[-1] <= f.x + f.w
    finally:
        target.stop()
