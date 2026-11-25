"""Privacy proofs (spec §8.5): nothing reaches any backend from a password field, a denylisted
app, an incognito window, a login page, a terminal, while paused.

The real `glanced` runs on a private Xvfb with the fake CLI backend selected (`fake_cli`): the
whole shield -> argv/stdin path is live, and `tests/fake_cli/claude` appends everything it is
ever sent to a log. Each test types a marker, waits past the analyze pause, and asserts

  * the gate says why (`status()["field"]["gated"]`),
  * the daemon's own copy of the field text is empty (the tap, `test_state`),
  * the marker is nowhere in what the backend received.

`test_control_positive` is the other half: the same marker in an ordinary field DOES arrive,
so a passing proof cannot be a broken tap.

    .venv/bin/pytest -m apps tests/acceptance/test_privacy_e2e.py -v
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from tests.acceptance.harness import (
    BROWSERS,
    Browser,
    BrowserPage,
    GlancedUnderTest,
    GtkApp,
    Typist,
    wait_for,
)
from tests.acceptance.test_capture_matrix import NO_GO

pytestmark = pytest.mark.apps

MARKER = "ZQXJ-marker Teh quick brown fox jump over the lazy dog today."
PAUSE_S = 3.0  # past the analyze pause


@pytest.fixture
def typist(session):
    t = Typist(session)
    yield t
    t.close()


@pytest.fixture
def make_glanced(session, tmp_path):
    made: list[GlancedUnderTest] = []

    def make(fake_mode: str = "analyze") -> GlancedUnderTest:
        g = GlancedUnderTest(session, tmp_path / f"g{len(made)}", backend="fake_cli",
                             fake_mode=fake_mode)
        made.append(g)
        return g.start()

    yield make
    for g in made:
        g.stop()


def _spy(g: GlancedUnderTest) -> str:
    return g.stdin_log.read_text()


def _focused(g: GlancedUnderTest, timeout_s: float = 15.0) -> dict:
    return wait_for(lambda: g.status().get("field"), timeout_s)


def assert_nothing_left(g: GlancedUnderTest, reason: str) -> None:
    """The gate named `reason`, the daemon holds no text, and no backend saw the marker."""
    field = wait_for(lambda: (g.status().get("field") or {}).get("gated") == reason, 15.0)
    assert field, (reason, g.status())
    time.sleep(PAUSE_S)
    assert g.status()["field"]["gated"] == reason
    snap = g.state()["snapshot"]
    assert snap is not None and snap["text"] == "", snap
    assert "ZQXJ" not in _spy(g), "the marker reached a backend"
    assert "ZQXJ" not in g.stderr_path.read_text()


def _gtk(session, typist, wm_class: str | None = None) -> GtkApp:
    app = GtkApp(session, typist, wm_class=wm_class)
    app.start()
    return app


def test_password_field_gtk(session, typist, make_glanced):
    app = _gtk(session, typist)
    g = make_glanced()
    try:
        _focused(g)
        typist.key("Tab", 2)  # TextView -> Entry -> the password entry
        wait_for(lambda: (g.status().get("field") or {}).get("gated") == "password", 10.0)
        typist.type(MARKER)
        assert_nothing_left(g, "password")
    finally:
        app.stop()


def _page(kind, page, session, typist, fixture_server, profile_root, incognito=False):
    if kind in NO_GO:
        pytest.skip(NO_GO[kind])
    if not os.access(BROWSERS[kind], os.X_OK) and shutil.which(kind) is None:
        pytest.skip(f"{kind} is not installed")
    server, base = fixture_server
    return BrowserPage(session, typist, server, Browser(kind, profile_root, session, server),
                       f"{base}/{page}", incognito=incognito)


def test_password_field_web(session, typist, make_glanced, fixture_server, profile_root):
    target = _page("chrome", "password.html", session, typist, fixture_server, profile_root)
    target.start()
    g = make_glanced()
    try:
        _focused(g, 30.0)
        typist.type(MARKER)
        assert_nothing_left(g, "password")
    finally:
        target.stop()


@pytest.mark.parametrize("wm_class", ["Remmina", "AnyDesk", "Seahorse", "KeePassXC"])
def test_denylisted_app(wm_class, session, typist, make_glanced):
    app = _gtk(session, typist, wm_class)
    g = make_glanced()
    try:
        _focused(g)
        typist.type(MARKER)
        assert_nothing_left(g, "app_denylist")
    finally:
        app.stop()


def test_incognito_chrome(session, typist, make_glanced, fixture_server, profile_root):
    target = _page("chrome", "textarea.html", session, typist, fixture_server, profile_root,
                   incognito=True)
    target.start()
    g = make_glanced()
    try:
        _focused(g, 30.0)
        typist.type(MARKER)
        assert_nothing_left(g, "incognito")
    finally:
        target.stop()


def test_private_firefox(session, typist, make_glanced, fixture_server, profile_root):
    target = _page("firefox", "textarea.html", session, typist, fixture_server, profile_root,
                   incognito=True)
    target.start()
    g = make_glanced()
    try:
        _focused(g, 30.0)
        typist.type(MARKER)
        assert_nothing_left(g, "incognito")
    finally:
        target.stop()


def test_login_url(session, typist, make_glanced, fixture_server, profile_root):
    target = _page("chrome", "login/", session, typist, fixture_server, profile_root)
    target.start()
    g = make_glanced()
    try:
        _focused(g, 30.0)
        typist.type(MARKER)  # the notes textarea on a /login/ page
        assert_nothing_left(g, "url_denylist")
    finally:
        target.stop()


def test_terminal_class(session, typist, make_glanced):
    app = _gtk(session, typist, "gnome-terminal-server")
    g = make_glanced()
    try:
        _focused(g)
        typist.type(MARKER)
        assert_nothing_left(g, "terminal")
    finally:
        app.stop()


def test_paused(session, typist, make_glanced, fixture_server, profile_root):
    target = _page("chrome", "textarea.html", session, typist, fixture_server, profile_root)
    target.start()
    g = make_glanced()
    try:
        _focused(g, 30.0)
        done = g.cli("pause")
        assert done.returncode == 0, done.stderr
        assert wait_for(lambda: g.status().get("paused") is True, 5.0)
        typist.type(MARKER)
        time.sleep(PAUSE_S)
        assert g.status()["paused"] is True
        assert "ZQXJ" not in _spy(g)
        assert _spy(g) == "", "a backend was called while paused"
    finally:
        target.stop()


def test_control_positive(session, typist, make_glanced, fixture_server, profile_root):
    """The marker typed into an ordinary field DOES reach the backend (the tap works), and a
    typed API key arrives only as a placeholder."""
    target = _page("chrome", "textarea.html", session, typist, fixture_server, profile_root)
    target.start()
    g = make_glanced()
    try:
        _focused(g, 30.0)
        key = "sk-ant-api03-" + "a1B2c3D4" * 6
        typist.type(MARKER + " Use " + key + " for it.")
        assert wait_for(lambda: "ZQXJ" in _spy(g), 15.0), (_spy(g), g.stderr_path.read_text())
        log = _spy(g)
        assert "[[GLANCE_S1]]" in log
        assert key not in log and "a1B2c3D4a1B2c3D4" not in log
        assert (g.status().get("field") or {}).get("gated") is None
    finally:
        target.stop()


def test_typed_text_never_in_argv(session, typist, make_glanced, fixture_server,
                                  profile_root):
    target = _page("chrome", "textarea.html", session, typist, fixture_server, profile_root)
    target.start()
    g = make_glanced("hang")  # the fake CLI sleeps 30 s: the call is in flight while we look
    try:
        _focused(g, 30.0)
        typist.type(MARKER)

        def cmdlines() -> list[str]:
            out = []
            for pid in g.descendants():
                try:
                    out.append(Path(f"/proc/{pid}/cmdline").read_bytes().replace(
                        b"\0", b" ").decode("utf-8", "replace"))
                except OSError:
                    continue
            return out

        # non-vacuous: the call really is running, as a descendant of glanced
        assert wait_for(lambda: any("fake_cli" in c for c in cmdlines()), 15.0), cmdlines()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            assert not any("ZQXJ" in c for c in cmdlines())
            time.sleep(0.05)
        assert "ZQXJ" in _spy(g)  # it did get the marker, through stdin
    finally:
        target.stop()


def test_select_anything_refuses_denylisted_app(session, typist, make_glanced):
    """The select-anything hotkey in a denylisted app opens nothing and sends nothing."""
    app = _gtk(session, typist, "Seahorse")
    g = make_glanced()
    try:
        _focused(g)
        typist.type(MARKER)
        typist.chord("Control_L", "a")
        time.sleep(0.4)
        typist.chord("Super_L", "Alt_L", "a")
        time.sleep(PAUSE_S)
        assert g.status().get("selection_open") is False, g.status()
        assert _spy(g) == ""
    finally:
        app.stop()


def test_select_anything_in_a_password_entry_is_refused(session, typist, make_glanced):
    """The window gate judges the window (class, title); the focused field decides too. In a
    GTK password entry of an allowed app the hotkey is refused before PRIMARY is read: no
    panel, nothing sent."""
    app = _gtk(session, typist)
    g = make_glanced()
    try:
        _focused(g)
        typist.key("Tab", 2)
        wait_for(lambda: (g.status().get("field") or {}).get("gated") == "password", 10.0)
        typist.type(MARKER)
        typist.chord("Control_L", "a")
        time.sleep(0.4)
        typist.chord("Super_L", "Alt_L", "a")
        time.sleep(PAUSE_S)
        assert g.status().get("selection_open") is False
        assert _spy(g) == ""
        assert "select_anything_refused reason=password" in g.stderr_path.read_text()
    finally:
        app.stop()
