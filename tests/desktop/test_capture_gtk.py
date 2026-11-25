"""glance-capture against a real GTK window. Run inside the GNOME session:

    .venv/bin/pytest -m desktop tests/desktop/test_capture_gtk.py -v

The test opens its own window, gives it focus and presses Tab twice in it. Keys are only
sent while that window is the active one.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from glance.protocol import SNAPSHOT, FieldSnapshot

pytestmark = pytest.mark.desktop

APP = Path(__file__).with_name("gtk_app.py")
TEXT = "Teh quick brown fox"


class Lines:
    """Collects a process's stdout lines on a thread."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self.lines: list[str] = []
        self._proc = proc
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            self.lines.append(line.rstrip("\n"))

    def wait_for(self, pred, timeout: float = 2.0, start: int = 0):
        deadline = time.monotonic() + timeout
        while True:
            for line in self.lines[start:]:
                hit = pred(line)
                if hit:
                    return hit
            if time.monotonic() > deadline:
                return None
            time.sleep(0.02)


def _snapshot(line: str) -> FieldSnapshot | None:
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    return FieldSnapshot.from_dict(obj["p"]) if obj.get("t") == SNAPSHOT else None


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            pytest.fail(f"pid {proc.pid} ignored SIGTERM; close it by hand")


class Desktop:
    """The little X11 work the test needs: activate a window, check it, press a key."""

    def __init__(self) -> None:
        import gi

        gi.require_version("Gdk", "3.0")
        gi.require_version("Wnck", "3.0")
        from gi.repository import Gdk, GLib, Wnck
        from Xlib import display

        Gdk.init([])
        self._wnck = Wnck
        self._glib = GLib
        self._dpy = display.Display()
        self._root = self._dpy.screen().root

    def _server_time(self) -> int:
        from Xlib import X, Xatom

        win = self._root.create_window(0, 0, 1, 1, 0, self._dpy.screen().root_depth,
                                       event_mask=X.PropertyChangeMask)
        try:
            win.change_property(Xatom.WM_NAME, Xatom.STRING, 8, b"")
            self._dpy.sync()
            while True:
                event = self._dpy.next_event()
                if event.type == X.PropertyNotify:
                    return int(event.time)
        finally:
            win.destroy()
            self._dpy.sync()

    def active_pid(self) -> int:
        from Xlib import X, error

        try:
            prop = self._root.get_full_property(
                self._dpy.intern_atom("_NET_ACTIVE_WINDOW"), X.AnyPropertyType)
            if not prop or not prop.value[0]:
                return 0
            win = self._dpy.create_resource_object("window", int(prop.value[0]))
            pid = win.get_full_property(self._dpy.intern_atom("_NET_WM_PID"), X.AnyPropertyType)
            return int(pid.value[0]) if pid else 0
        except error.XError:
            return 0

    def activate(self, pid: int, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.active_pid() == pid:
                return True
            # No main loop runs here: let Wnck hear about the new window first.
            context = self._glib.MainContext.default()
            while context.pending():
                context.iteration(False)
            screen = self._wnck.Screen.get_default()
            screen.force_update()
            for win in screen.get_windows():
                if win.get_pid() == pid:
                    win.activate(self._server_time())
            time.sleep(0.1)
        return self.active_pid() == pid

    def press(self, keysym_name: str, pid: int) -> None:
        from Xlib import XK, X
        from Xlib.ext import xtest

        assert self.active_pid() == pid, "test window is not active; no key sent"
        code = self._dpy.keysym_to_keycode(XK.string_to_keysym(keysym_name))
        xtest.fake_input(self._dpy, X.KeyPress, code)
        xtest.fake_input(self._dpy, X.KeyRelease, code)
        self._dpy.sync()


@pytest.fixture
def session():
    if not os.environ.get("DISPLAY"):
        pytest.skip("no X display")
    env = {**os.environ, "GLANCE_TEST": "1", "GLANCE_STRICT_LOG": "0"}
    app = subprocess.Popen([sys.executable, str(APP), "--text", TEXT],
                           stdout=subprocess.PIPE, text=True)
    capture = None
    try:
        app_out = Lines(app)
        assert app_out.wait_for(lambda line: line == "READY", timeout=10), "gtk_app not ready"
        capture = subprocess.Popen(
            [sys.executable, "-m", "glance.capture.main", "--print", "--with-text-for-tests"],
            stdout=subprocess.PIPE, text=True, env=env)
        desktop = Desktop()
        assert desktop.activate(app.pid), "could not give the test window focus"
        yield app, Lines(capture), desktop
    finally:
        if capture is not None:
            _stop(capture)
        _stop(app)


def test_gtk_textview_snapshot(session):
    _app, out, _desktop = session

    def good(line: str):
        s = _snapshot(line)
        return s if s is not None and s.text == TEXT else None

    s = out.wait_for(good, timeout=2.0)
    assert s is not None, f"no snapshot of the TextView; got {len(out.lines)} lines"
    assert s.gated is None and s.caret == len(TEXT)
    assert s.caret_rect is not None and s.caret_rect.h > 0
    assert s.capture_confidence == 1.0
    assert "gtk_app" in s.wm_class.casefold()
    assert s.title == "glance-test-app"
    assert s.window_rect is not None and s.field_rect is not None


def test_gtk_password_entry_gated(session):
    app, out, desktop = session
    assert out.wait_for(lambda line: _snapshot(line) is not None, timeout=2.0)
    desktop.press("Tab", app.pid)
    time.sleep(0.15)
    desktop.press("Tab", app.pid)

    def gated(line: str):
        s = _snapshot(line)
        return s if s is not None and s.gated == "password" else None

    assert out.wait_for(gated, timeout=2.0), "password entry never reported"
    time.sleep(0.3)
    last = [s for s in map(_snapshot, out.lines) if s is not None][-1]
    assert last.gated == "password" and last.text == ""
    assert last.caret == 0 and last.caret_rect is None


def test_hung_app_raises_timeout_within_budget(session):
    """A stopped application must surface as A11yTimeout, quickly, and never as empty text."""
    from glance.capture.atspi_port import AtspiPort
    from glance.capture.ports import A11yTimeout

    app, out, _desktop = session
    assert out.wait_for(lambda line: _snapshot(line) is not None, timeout=2.0)
    port = AtspiPort()
    field = port.focused()
    assert field is not None, "focused TextView not found by the tree search"
    assert field.read().text == TEXT
    assert field.meta().role == "text"
    os.kill(app.pid, signal.SIGSTOP)
    try:
        for call in (field.meta, field.read):
            t0 = time.monotonic()
            with pytest.raises(A11yTimeout):
                call()
            assert time.monotonic() - t0 < 0.6  # one or two 200 ms AT-SPI timeouts
    finally:
        os.kill(app.pid, signal.SIGCONT)
    assert field.read().text == TEXT
    assert field.set_selection(0, 3) and field.read().selection == (0, 3)
    assert field.set_caret(len(TEXT)) and field.read().caret == len(TEXT)
