"""Tone chips end to end: glanced + glance-capture against a real GTK text view.

Everything runs on a private Xvfb server with its own D-Bus session (and so its own
accessibility bus) and a minimal window manager (`mini_wm.py`): the display the user works
on is never touched, and the keys the tests press go to that server only. The AI is the
daemon's test backend (GLANCE_TEST=1 + GLANCE_FAKE_BACKEND=1): no real call is made.

The tests share one session and run in file order. They take ~20 s and depend on timing, so
they are marked `xvfb` and left out of the default run: `.venv/bin/pytest -m xvfb`.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.desktop.test_capture_gtk import Lines

HERE = Path(__file__).parent
TEXT = "ok send me the report today, i need it before the meeting starts."
REWRITE = "Thanks so much, will do!"
APP_TITLE = "glance-test-app"
HARPER = Path.home() / ".local/share/glance/bin/harper-ls"

pytestmark = [
    pytest.mark.xvfb,
    pytest.mark.skipif(shutil.which("Xvfb") is None, reason="Xvfb is not installed"),
    pytest.mark.skipif(shutil.which("dbus-daemon") is None, reason="dbus-daemon is missing"),
]


def _wait(pred, timeout: float, step: float = 0.03):
    deadline = time.monotonic() + timeout
    while True:
        value = pred()
        if value or time.monotonic() > deadline:
            return value
        time.sleep(step)


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()  # a process of this test's own private session
            proc.wait(timeout=5)


class Screen:
    """The test's own connection to the Xvfb server: keys in, active window out."""

    def __init__(self, name: str) -> None:
        from Xlib import display

        self.d = display.Display(name)
        self.root = self.d.screen().root

    def _key(self, name: str) -> int:
        from Xlib import XK

        code = self.d.keysym_to_keycode(XK.string_to_keysym(name))
        assert code, name
        return code

    def chord(self, *keys: str) -> None:
        from Xlib import X
        from Xlib.ext import xtest

        codes = [self._key(k) for k in keys]
        for code in codes:
            xtest.fake_input(self.d, X.KeyPress, code)
        for code in reversed(codes):
            xtest.fake_input(self.d, X.KeyRelease, code)
        self.d.sync()

    def type(self, text: str) -> None:
        for ch in text:
            self.chord("space" if ch == " " else ch)
            time.sleep(0.01)

    def activate(self, pid: int) -> None:
        """Ask the window manager, as a pager would, to activate the window of `pid`."""
        from Xlib import X
        from Xlib.protocol import event

        atom = self.d.intern_atom
        clients = self.root.get_full_property(atom("_NET_CLIENT_LIST"), X.AnyPropertyType)
        for xid in clients.value if clients else ():
            win = self.d.create_resource_object("window", int(xid))
            owner = win.get_full_property(atom("_NET_WM_PID"), X.AnyPropertyType)
            if owner and int(owner.value[0]) == pid:
                message = event.ClientMessage(
                    window=win, client_type=atom("_NET_ACTIVE_WINDOW"),
                    data=(32, [2, X.CurrentTime, 0, 0, 0]))
                self.root.send_event(
                    message, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
                self.d.sync()
                return
        raise AssertionError(f"no window of pid {pid}")

    def active(self) -> tuple[int, str]:
        """(pid, title) of the active window; (0, "") without one."""
        from Xlib import X, error

        try:
            prop = self.root.get_full_property(self.d.intern_atom("_NET_ACTIVE_WINDOW"),
                                               X.AnyPropertyType)
            if not prop or not prop.value[0]:
                return 0, ""
            win = self.d.create_resource_object("window", int(prop.value[0]))
            pid = win.get_full_property(self.d.intern_atom("_NET_WM_PID"), X.AnyPropertyType)
            return (int(pid.value[0]) if pid else 0), (win.get_wm_name() or "")
        except error.XError:
            return 0, ""


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    from glance.ipc import JsonLineClient

    home = tmp_path_factory.mktemp("tone")
    dirs = {name: home / name for name in ("rt", "cfg", "data")}
    for d in dirs.values():
        d.mkdir(mode=0o700)
    if HARPER.exists():
        (dirs["data"] / "glance" / "bin").mkdir(parents=True)
        (dirs["data"] / "glance" / "bin" / "harper-ls").symlink_to(HARPER)
    procs: list[subprocess.Popen] = []
    log = (home / "glanced.stderr").open("w")
    try:
        read_fd, write_fd = os.pipe()
        procs.append(subprocess.Popen(
            ["Xvfb", "-displayfd", str(write_fd), "-screen", "0", "1280x800x24", "-nolisten",
             "tcp"], pass_fds=[write_fd], stderr=subprocess.DEVNULL))
        os.close(write_fd)
        with os.fdopen(read_fd) as f:
            number = f.readline().strip()
        assert number, "Xvfb did not start"
        env = {**os.environ, "DISPLAY": f":{number}", "GDK_BACKEND": "x11",
               "XDG_SESSION_TYPE": "x11",
               "XDG_RUNTIME_DIR": str(dirs["rt"]), "XDG_CONFIG_HOME": str(dirs["cfg"]),
               "XDG_DATA_HOME": str(dirs["data"]), "GLANCE_STRICT_LOG": "1",
               "GLANCE_TEST": "1", "GLANCE_FAKE_BACKEND": "1"}
        for name in ("WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "AT_SPI_BUS_ADDRESS",
                     "NO_AT_BRIDGE"):
            env.pop(name, None)
        bus = subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--print-address=1"],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True)
        procs.append(bus)
        assert bus.stdout is not None
        env["DBUS_SESSION_BUS_ADDRESS"] = bus.stdout.readline().strip()
        assert env["DBUS_SESSION_BUS_ADDRESS"], "dbus-daemon did not start"

        deaf = home / "wm-ignores-activation"
        wm = subprocess.Popen([sys.executable, str(HERE / "mini_wm.py"), str(deaf)], env=env,
                              stdout=subprocess.PIPE, text=True)
        procs.append(wm)
        assert Lines(wm).wait_for(lambda line: line == "READY", timeout=10), "no window manager"
        app = subprocess.Popen([sys.executable, str(HERE / "gtk_app.py"), "--text", TEXT,
                                "--undo"], env=env, stdout=subprocess.PIPE, text=True)
        procs.append(app)
        out = Lines(app)
        assert out.wait_for(lambda line: line == "READY", timeout=15), "gtk_app not ready"
        # The daemon's own windows stay off the accessibility bus, as when it is installed.
        daemon = subprocess.Popen([sys.executable, "-m", "glance.daemon.main"],
                                  env={**env, "NO_AT_BRIDGE": "1"}, stderr=log,
                                  stdout=subprocess.DEVNULL)
        procs.append(daemon)
        sock = dirs["rt"] / "glance" / "ctl.sock"

        def request(msg_type: str, payload: dict | None = None) -> dict:
            client = JsonLineClient(sock)
            try:
                client.connect(timeout_s=0)
                return client.request(msg_type, payload or {})
            except (ConnectionError, TimeoutError):
                return {}
            finally:
                client.close()

        def status() -> dict:
            return request("status")

        def buffer() -> str:
            seen = len(out.lines)
            app.send_signal(signal.SIGUSR1)
            line = out.wait_for(lambda text: text, timeout=3.0, start=seen)
            assert line is not None, "gtk_app did not print its buffer"
            return json.loads(line)

        def field_is(text: str) -> bool:
            st = status()
            return bool(st.get("field")) and st["field"]["len"] == len(text) \
                and st.get("ai") == "ok"

        screen = Screen(env["DISPLAY"])
        log_path = home / "glanced.stderr"
        assert _wait(lambda: field_is(TEXT), 20), (status(), log_path.read_text()[-2000:])
        def clipboard() -> str:
            code = ("import gi; gi.require_version('Gtk', '3.0'); gi.require_version('Gdk', "
                    "'3.0'); from gi.repository import Gdk, Gtk; "
                    "print(Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).wait_for_text() or '')")
            done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                  env={**env, "NO_AT_BRIDGE": "1"}, timeout=15, check=False)
            return done.stdout.rstrip("\n")

        yield SimpleNamespace(request=request, status=status, buffer=buffer, screen=screen,
                              field_is=field_is, app=app, daemon=daemon, dirs=dirs,
                              log_path=log_path, deaf=deaf, clipboard=clipboard)
    finally:
        for proc in reversed(procs):
            _stop(proc)
        log.close()


def test_tone_hotkey_rewrites_gtk_field(session):
    st = session.status()
    assert st["hotkeys_unavailable"] is None and st["backend"] == "fake", st
    assert session.screen.active()[1] == APP_TITLE and session.buffer() == TEXT
    session.screen.chord("Super_L", "Alt_L", "1")
    assert _wait(lambda: session.buffer() == REWRITE, 2.0), session.buffer()
    # One undo step gives the text back.
    assert _wait(lambda: session.field_is(REWRITE), 3.0), session.status()
    session.screen.chord("Control_L", "z")
    assert _wait(lambda: session.buffer() == TEXT, 2.0), session.buffer()


def test_rewrite_as_returns_focus(session):
    assert _wait(lambda: session.field_is(TEXT), 5.0), session.status()
    assert session.request("test_panel_action", {"action": "tone",
                                                 "args": {"key": "custom"}}).get("ok") is True
    # The entry is a window of the daemon and it has the keyboard.
    assert _wait(lambda: session.screen.active()[0] == session.daemon.pid, 3.0), \
        session.screen.active()
    session.screen.type("as a pirate")
    session.screen.chord("Return")
    assert _wait(lambda: session.screen.active() == (session.app.pid, APP_TITLE), 2.0), \
        session.screen.active()
    # Back in the field, and only then: the rewrite is typed into it.
    assert _wait(lambda: session.buffer() == REWRITE, 5.0), (session.buffer(), session.status())
    assert session.screen.active() == (session.app.pid, APP_TITLE)
    assert _wait(lambda: session.field_is(REWRITE), 3.0)
    session.screen.chord("Control_L", "z")
    assert _wait(lambda: session.buffer() == TEXT, 2.0), session.buffer()


def test_rewrite_as_escape_returns_focus_and_changes_nothing(session):
    assert _wait(lambda: session.field_is(TEXT), 5.0), session.status()
    session.request("test_panel_action", {"action": "tone", "args": {"key": "custom"}})
    assert _wait(lambda: session.screen.active()[0] == session.daemon.pid, 3.0)
    session.screen.type("as a pirate")
    session.screen.chord("Escape")
    assert _wait(lambda: session.screen.active() == (session.app.pid, APP_TITLE), 2.0)
    time.sleep(1.0)
    assert session.buffer() == TEXT


def test_rewrite_as_does_not_apply_when_the_window_does_not_come_back(session):
    assert _wait(lambda: session.field_is(TEXT), 5.0), session.status()
    session.request("test_panel_action", {"action": "tone", "args": {"key": "custom"}})
    assert _wait(lambda: session.screen.active()[0] == session.daemon.pid, 3.0)
    session.deaf.touch()  # from now on the window manager refuses to activate anything
    try:
        session.screen.type("as a pirate")
        session.screen.chord("Return")
        # The entry is gone, the target window did not get the focus back: wait out the
        # 1.5 s Glance gives it, and more. Nothing may be typed anywhere.
        assert _wait(lambda: session.screen.active()[0] != session.daemon.pid, 2.0)
        time.sleep(2.5)
        assert session.screen.active()[0] != session.app.pid
        assert session.buffer() == TEXT
    finally:
        session.deaf.unlink()
    # The user goes back by hand: the rewrite is shown and offered as a copy, not typed.
    session.screen.activate(session.app.pid)
    assert _wait(lambda: session.field_is(TEXT), 5.0), session.status()
    time.sleep(1.0)
    assert session.buffer() == TEXT
    session.request("test_panel_action", {"action": "apply_tone", "args": {}})
    time.sleep(0.5)
    assert session.buffer() == TEXT
    session.request("test_panel_action", {"action": "copy", "args": {}})
    assert _wait(lambda: session.clipboard() == REWRITE, 5.0, 0.3), session.clipboard()
    assert session.buffer() == TEXT


def test_nothing_typed_reaches_the_disk(session):
    paths = [str(d) for d in session.dirs.values()] + [str(session.log_path)]
    for needle in ("send me the report", "Thanks so much", "as a pirate"):
        done = subprocess.run(["grep", "-r", "-l", needle, *paths], capture_output=True,
                              text=True, check=False)
        assert done.returncode == 1 and done.stdout == "", done.stdout
    log = session.log_path.read_text()
    assert "Traceback" not in log and "ai_test_backend" in log


# ---------------------------------------------------------------- the panel's tone part


@pytest.fixture(scope="module")
def probe():
    """What `tone_probe.py` saw on an Xvfb of its own (no window manager, no other client)."""
    read_fd, write_fd = os.pipe()
    xvfb = subprocess.Popen(
        ["Xvfb", "-displayfd", str(write_fd), "-screen", "0", "1280x800x24", "-nolisten",
         "tcp"], pass_fds=[write_fd], stderr=subprocess.DEVNULL)
    os.close(write_fd)
    try:
        with os.fdopen(read_fd) as f:
            number = f.readline().strip()
        assert number, "Xvfb did not start"
        env = {**os.environ, "DISPLAY": f":{number}", "NO_AT_BRIDGE": "1", "GDK_BACKEND": "x11"}
        env.pop("WAYLAND_DISPLAY", None)
        done = subprocess.run([sys.executable, str(HERE / "tone_probe.py")], env=env,
                              capture_output=True, text=True, timeout=60, check=False)
        assert done.returncode == 0, done.stderr[-2000:]
        yield json.loads(done.stdout.strip().splitlines()[-1])
    finally:
        _stop(xvfb)


CARDS = ["Friendlier | 1 | Hover to preview", "More direct | 2 | Hover to preview",
         "Shorter | 3 | Hover to preview", "Professional | 4 | Hover to preview",
         "e.g. make it a bullet list"]


def test_drawer_shows_the_profile_and_the_tone_cards_with_their_hotkeys(probe):
    assert probe["chips_only"] == ["For Team channel ▾", "Close", *CARDS]
    # a card click applies: there is no separate Apply button, loading or ready
    assert probe["loading"] == probe["chips_only"] == probe["ready"]


def test_resting_on_a_card_asks_for_its_preview_passing_over_does_not(probe):
    assert probe["after_passing_over"] == []
    # "Rewrite as…" was rested on too: it only ever starts on a click.
    assert probe["after_resting"] == [["tone_hover", {"key": "shorter"}]]


def test_drawer_clicks_reach_the_tone_actions(probe):
    assert probe["clicks"] == [
        ["tone_apply", {"key": "shorter"}],
        ["tone", {"key": "custom"}],
        ["profile", {"name": "email"}],
    ]
    assert probe["profiles_open"] == ["For Team channel ▾", "Close", "Team channel",
                                      "Direct message", "Email", *CARDS]
    assert probe["profiles_closed"] == probe["ready"]


def test_a_rewrite_that_cannot_be_applied_is_only_offered_as_a_copy(probe):
    assert probe["blocked"] == [*probe["chips_only"], "Copy rewrite"]
    assert probe["blocked_clicks"] == [["copy", {}]]


def test_tone_drawer_fits_and_never_takes_the_focus(probe):
    assert probe["ready_size"][0] == 460 and probe["ready_size"][1] <= 480
    assert probe["blocked_size"][0] == 460 and probe["blocked_size"][1] <= 480  # 900 words
    assert probe["focus_before"] and probe["focus_after"] and probe["panel_active"] is False
