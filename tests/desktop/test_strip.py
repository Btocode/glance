"""The status icon and the suggestion strip end to end: glanced + glance-capture + harper-ls against a real GTK
text view, on a private Xvfb with its own D-Bus session and a minimal window manager. The
display the user works on is never touched; keys and clicks go to that server only. The AI
is the daemon's test backend (no real call).

    .venv/bin/pytest -m xvfb tests/desktop/test_strip.py

The tests share one session and run in file order (~40 s). The default `[ui] suggestions =
"on_click"` holds until the last tests switch it to "auto": the strip is opened by a click on
the status icon until then.
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
from tests.desktop.test_panel_tone import Screen, _stop, _wait

HERE = Path(__file__).parent
TEXT = "We needs a plan for this week and it is fine."
FIXED = "We need a plan for this week and it is fine."
CLEAN = "We have a plan for this week and it is fine."
HARPER = Path.home() / ".local/share/glance/bin/harper-ls"

pytestmark = [
    pytest.mark.xvfb,
    pytest.mark.skipif(shutil.which("Xvfb") is None, reason="Xvfb is not installed"),
    pytest.mark.skipif(shutil.which("dbus-daemon") is None, reason="dbus-daemon is missing"),
    pytest.mark.skipif(not HARPER.exists(), reason="harper-ls is not installed"),
]


@pytest.fixture(scope="module")
def s(tmp_path_factory):
    from glance.ipc import JsonLineClient

    home = tmp_path_factory.mktemp("strip")
    dirs = {name: home / name for name in ("rt", "cfg", "data")}
    for d in dirs.values():
        d.mkdir(mode=0o700)
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
        env["DBUS_SESSION_BUS_ADDRESS"] = bus.stdout.readline().strip()
        assert env["DBUS_SESSION_BUS_ADDRESS"], "dbus-daemon did not start"
        wm = subprocess.Popen([sys.executable, str(HERE / "mini_wm.py")], env=env,
                              stdout=subprocess.PIPE, text=True)
        procs.append(wm)
        assert Lines(wm).wait_for(lambda line: line == "READY", timeout=10), "no window manager"
        app = subprocess.Popen([sys.executable, str(HERE / "gtk_app.py"), "--text", TEXT,
                                "--undo"], env=env, stdout=subprocess.PIPE, text=True)
        procs.append(app)
        out = Lines(app)
        assert out.wait_for(lambda line: line == "READY", timeout=15), "gtk_app not ready"
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

        def buffer() -> str:
            seen = len(out.lines)
            app.send_signal(signal.SIGUSR1)
            line = out.wait_for(lambda text: text, timeout=3.0, start=seen)
            assert line is not None, "gtk_app did not print its buffer"
            return json.loads(line)

        screen = Screen(env["DISPLAY"])
        log_path = home / "glanced.stderr"
        yield SimpleNamespace(request=request, buffer=buffer, screen=screen, app=app,
                              daemon=daemon, log_path=log_path,
                              config=dirs["cfg"] / "glance" / "config.toml",
                              overlay=lambda: request("test_overlay"),
                              snapshot=lambda: (request("test_state") or {}).get("snapshot"))
    finally:
        for proc in reversed(procs):
            _stop(proc)
        log.close()


def _app_window(s):
    from Xlib import X

    d, atom = s.screen.d, s.screen.d.intern_atom
    clients = s.screen.root.get_full_property(atom("_NET_CLIENT_LIST"), X.AnyPropertyType)
    for xid in clients.value if clients else ():
        win = d.create_resource_object("window", int(xid))
        owner = win.get_full_property(atom("_NET_WM_PID"), X.AnyPropertyType)
        if owner and int(owner.value[0]) == s.app.pid:
            return win
    raise AssertionError("no app window")


def _click(s, x: int, y: int) -> None:
    from Xlib import X
    from Xlib.ext import xtest

    xtest.fake_input(s.screen.d, X.MotionNotify, x=x, y=y)
    xtest.fake_input(s.screen.d, X.ButtonPress, 1)
    xtest.fake_input(s.screen.d, X.ButtonRelease, 1)
    s.screen.d.sync()


def _icon(s):
    return _wait(lambda: s.overlay().get("status"), 10.0)


def _click_icon(s) -> None:
    x, y, w, h = _icon(s)
    _click(s, x + w // 2, y + h // 2)


def _strip(s):
    """The strip's geometry, opened with a click on the status icon when it is not up."""
    if not s.overlay().get("strip"):
        assert _wait(lambda: (s.overlay().get("status_state") or {}).get("state") == "ready",
                     10.0), s.overlay()
        _click_icon(s)
    return _wait(lambda: s.overlay().get("strip"), 10.0)


def _state(s):
    return (s.overlay().get("status_state") or {}).get("state")


def test_status_icon_appears_on_focus_at_the_caret_line(s):
    assert _icon(s), (s.overlay(), s.log_path.read_text()[-2000:])
    x, y, w, h = s.overlay()["status"]
    snap = s.snapshot()
    field, caret = snap["field_rect"], snap["caret_rect"]
    assert (w, h) == (22, 22)
    assert abs(y + h / 2 - (caret["y"] + caret["h"] / 2)) <= 1
    right = field["x"] + field["w"]
    assert right - w - 4 <= x <= right + 4  # at the field's right edge, inside or just out
    assert not x <= caret["x"] <= x + w or not y <= caret["y"] <= y + h  # never on the caret


def test_status_icon_shows_working_then_ready(s):
    assert _wait(lambda: _state(s) == "ready", 10.0), s.overlay()
    assert s.overlay()["status_state"]["badge"] == "1"
    s.screen.chord("End")
    s.screen.chord("space")
    seen = _wait(lambda: _state(s) == "working", 2.0, step=0.01)
    assert seen, s.overlay()
    # while keys may keep coming the ring stands still: nothing is redrawn per keystroke
    assert s.overlay()["status_state"]["spinning"] is False
    s.screen.chord("BackSpace")
    assert _wait(lambda: _state(s) == "ready", 5.0), s.overlay()
    assert s.overlay()["status_state"] == {"state": "ready", "badge": "1", "spinning": False}
    assert s.buffer() == TEXT


def test_strip_is_hidden_until_the_icon_is_clicked(s):
    assert _wait(lambda: _state(s) == "ready", 5.0)
    time.sleep(1.0)  # well past the settle time: it would have shown by now
    o = s.overlay()
    assert o["strip"] is None and o["panel"] is None
    assert o["enter_grabbed"] is False and o["escape_grabbed"] is False


def test_icon_click_opens_the_strip_and_the_focus_stays_in_the_app(s):
    _click_icon(s)
    assert _wait(lambda: s.overlay().get("strip"), 3.0), s.overlay()
    o = s.overlay()
    assert o["strip_text"] == FIXED and o["can_use"] is True and o["panel"] is None
    assert o["enter_grabbed"] is True and o["escape_grabbed"] is True
    assert s.screen.active()[0] == s.app.pid
    _click_icon(s)  # again: closed
    assert _wait(lambda: s.overlay().get("strip") is None, 3.0), s.overlay()
    assert s.overlay()["enter_grabbed"] is False
    assert s.screen.active()[0] == s.app.pid


def test_escape_closes_the_opened_strip(s):
    _click_icon(s)
    assert _wait(lambda: s.overlay().get("strip"), 3.0), s.overlay()
    s.screen.chord("Escape")
    assert _wait(lambda: s.overlay().get("strip") is None, 3.0), s.overlay()
    assert _wait(lambda: not s.overlay().get("escape_grabbed"), 3.0)
    assert _wait(lambda: _state(s) == "ready", 3.0)  # the icon stays (after the grab blur)
    assert s.buffer() == TEXT


def test_the_icon_follows_a_window_move(s):
    x, y, _w, _h = _icon(s)
    win = _app_window(s)
    g = win.get_geometry()
    win.configure(x=g.x + 60, y=g.y + 40)
    s.screen.d.sync()

    def followed():
        st = s.overlay().get("status")
        return st if st and abs(st[0] - (x + 60)) <= 3 and abs(st[1] - (y + 40)) <= 3 else None

    assert _wait(followed, 2.0), (s.overlay(), (x, y))
    win.configure(x=g.x, y=g.y)
    s.screen.d.sync()
    assert _wait(lambda: (st := s.overlay().get("status")) and abs(st[0] - x) <= 3, 2.0)
    assert s.screen.active()[0] == s.app.pid


def test_collapsed_strip_shows_only_the_corrected_sentence(s):
    assert _strip(s), (s.overlay(), s.log_path.read_text()[-2000:])
    o = s.overlay()
    assert o["strip_text"] == FIXED and o["can_use"] is True
    assert o["panel"] is None  # collapsed: nothing else on screen
    snap = s.snapshot()
    field, caret = snap["field_rect"], snap["caret_rect"]
    x, y, w, _h = o["strip"]
    assert x == field["x"] and w == max(320, min(field["w"], 720))
    assert caret["y"] + caret["h"] <= y <= caret["y"] + caret["h"] + 10


def test_more_expands_and_the_focus_stays_in_the_app(s):
    x, y, w, h = _strip(s)
    _click(s, x + w - 24, y + h // 2)  # the "⋯" button, at the strip's right end
    assert _wait(lambda: s.overlay().get("panel"), 3.0), s.overlay()
    _px, py, _pw, _ph = s.overlay()["panel"]
    assert py >= y + h - 20  # under the strip
    assert s.screen.active()[0] == s.app.pid  # the click never took the focus
    _click(s, x + w - 24, y + h // 2)
    assert _wait(lambda: s.overlay().get("panel") is None, 3.0), s.overlay()
    assert s.screen.active()[0] == s.app.pid


def test_window_move_and_resize_the_strip_follows(s):
    x, y, w, _h = _strip(s)
    win = _app_window(s)
    g = win.get_geometry()
    win.configure(x=g.x + 120, y=g.y + 80)
    s.screen.d.sync()
    def followed():
        st = s.overlay().get("strip")
        return st if st and abs(st[0] - (x + 120)) <= 3 and abs(st[1] - (y + 80)) <= 3 else None

    moved = _wait(followed, 2.0)
    assert moved, (s.overlay(), (x, y))
    win.configure(width=g.width + 160, height=g.height + 60)
    s.screen.d.sync()

    def settled():
        st, snap = s.overlay().get("strip"), s.snapshot()
        if not st or not snap or not snap.get("field_rect"):
            return None
        f, c = snap["field_rect"], snap["caret_rect"]
        ok = (abs(st[0] - f["x"]) <= 3 and abs(st[2] - max(320, min(f["w"], 720))) <= 3
              and c["y"] + c["h"] <= st[1] <= c["y"] + c["h"] + 10)
        return st if ok and f["w"] > w else None

    assert _wait(settled, 3.0), (s.overlay(), {k: v for k, v in (s.snapshot() or {}).items()
                                               if k.endswith("rect")}, w)
    assert s.screen.active()[0] == s.app.pid


def test_plain_enter_is_the_app_s_while_the_strip_is_up(s):
    """Enter is "send" in chat apps: the strip never takes it, only Alt+Enter."""
    assert _wait(lambda: s.overlay().get("enter_grabbed"), 5.0), s.overlay()
    s.screen.chord("Control_L", "End")
    s.screen.chord("Return")
    assert _wait(lambda: s.buffer() == TEXT + "\n", 3.0), repr(s.buffer())
    s.screen.chord("BackSpace")
    assert _wait(lambda: s.buffer() == TEXT, 3.0), repr(s.buffer())


def test_alt_enter_uses_the_suggestion_in_one_undo_step(s):
    assert _wait(lambda: s.overlay().get("enter_grabbed"), 10.0), s.overlay()
    assert s.overlay()["strip_text"] == FIXED
    s.screen.chord("Alt_L", "Return")
    assert _wait(lambda: s.buffer() == FIXED, 5.0), s.buffer()
    s.screen.chord("Control_L", "z")
    assert _wait(lambda: s.buffer() == TEXT, 3.0), s.buffer()


def test_use_button_applies(s):
    x, y, w, h = _strip(s)
    assert s.overlay()["strip_text"] == FIXED
    _click(s, x + w - 80, y + h // 2)  # the "Alt+Enter" chip, left of the "⋯"
    assert _wait(lambda: s.buffer() == FIXED, 5.0), (s.buffer(), s.overlay())
    assert s.screen.active()[0] == s.app.pid


def test_nothing_to_fix_shows_nothing(s):
    assert _wait(lambda: s.overlay().get("strip") is None, 5.0), s.overlay()
    assert s.overlay()["enter_grabbed"] is False
    # and Alt+Return is let go again; plain Return always was the app's: a new line
    s.screen.chord("Control_L", "End")
    s.screen.chord("Return")
    assert _wait(lambda: s.buffer() == FIXED + "\n", 3.0), repr(s.buffer())


def test_auto_mode_shows_the_strip_without_a_click(s):
    s.config.parent.mkdir(parents=True, exist_ok=True)
    s.config.write_text('[ui]\nsuggestions = "auto"\n')
    time.sleep(1.5)  # the config is polled once a second
    s.screen.chord("Control_L", "End")
    s.screen.type("we needs a plan for this week")
    got = _wait(lambda: s.overlay().get("strip"), 10.0)
    assert got, s.overlay()
    o = s.overlay()
    assert o["can_use"] is True and "need a plan" in o["strip_text"]
    assert o["status_state"]["state"] == "ready"
    assert o["escape_grabbed"] is False  # shown by itself: Esc stays the app's
    assert s.screen.active()[0] == s.app.pid


def test_a_bad_suggestions_value_falls_back_to_on_click(s):
    s.config.write_text('[ui]\nsuggestions = "sometimes"\n')
    time.sleep(1.5)
    s.screen.chord("space")
    s.screen.chord("BackSpace")
    assert _wait(lambda: _state(s) == "ready", 5.0), s.overlay()
    time.sleep(0.8)
    assert s.overlay()["strip"] is None
    assert "config_value_ignored key=ui.suggestions" in s.log_path.read_text()
    assert "sometimes" not in s.log_path.read_text()


def test_no_traceback(s):
    assert "Traceback" not in s.log_path.read_text()
