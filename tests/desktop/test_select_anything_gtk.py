"""Select-anything against a real GTK window, on a private Xvfb server with a minimal window
manager. The display the user works on is never touched: the in-process Xlib adapters connect
to the Xvfb (DISPLAY is pointed at it for the module), and the keys go to it through XTEST.

Not here: the daemon-level flows (hotkey, panel clicks, verified apply) wait for the daemon
wiring of `SelectAnything`; the logic behind them is covered by tests/test_select_anything.py.
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

from glance.config import AppCfg
from tests.desktop.test_capture_gtk import Lines
from tests.desktop.test_panel_tone import Screen, _stop, _wait

pytestmark = [
    pytest.mark.desktop,
    pytest.mark.skipif(shutil.which("Xvfb") is None, reason="Xvfb is not installed"),
]

HERE = Path(__file__).parent
TEXT = "pls review teh patch"
REWRITE = "Please review the patch."


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    from glance.ui.selection_xlib import XlibSelection
    from glance.writeback.clipboard_xlib import XlibClipboard
    from glance.writeback.engine import WriteBackEngine
    from glance.writeback.keys_xlib import XlibKeys

    home = tmp_path_factory.mktemp("select")
    procs: list[subprocess.Popen] = []
    read_fd, write_fd = os.pipe()
    procs.append(subprocess.Popen(
        ["Xvfb", "-displayfd", str(write_fd), "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
        pass_fds=[write_fd], stderr=subprocess.DEVNULL))
    os.close(write_fd)
    with os.fdopen(read_fd) as f:
        number = f.readline().strip()
    assert number, "Xvfb did not start"
    env = {**os.environ, "DISPLAY": f":{number}", "GDK_BACKEND": "x11", "NO_AT_BRIDGE": "1"}
    env.pop("WAYLAND_DISPLAY", None)
    saved_display = os.environ.get("DISPLAY")
    os.environ["DISPLAY"] = env["DISPLAY"]
    sel = keys = clip = None
    try:
        wm = subprocess.Popen([sys.executable, str(HERE / "mini_wm.py"), str(home / "deaf")],
                              env=env, stdout=subprocess.PIPE, text=True)
        procs.append(wm)
        assert Lines(wm).wait_for(lambda line: line == "READY", timeout=10), "no window manager"
        app = subprocess.Popen(
            [sys.executable, str(HERE / "gtk_app.py"), "--text", TEXT, "--undo",
             "--wm-class", "gtk_app"], env=env, stdout=subprocess.PIPE, text=True)
        procs.append(app)
        out = Lines(app)
        assert out.wait_for(lambda line: line == "READY", timeout=15), "gtk_app not ready"
        screen = Screen(env["DISPLAY"])
        assert _wait(lambda: screen.active()[0] == app.pid, 5.0), screen.active()
        sel, keys, clip = XlibSelection(), XlibKeys(), XlibClipboard()
        sleeps: list[float] = []
        engine = WriteBackEngine(lambda: None, keys, clip, lambda wm_class: AppCfg(),
                                 lambda s: (sleeps.append(s), time.sleep(s)))

        def buffer() -> str:
            seen = len(out.lines)
            app.send_signal(signal.SIGUSR1)
            line = out.wait_for(lambda text: text, timeout=3.0, start=seen)
            assert line is not None, "gtk_app did not print its buffer"
            return json.loads(line)

        yield SimpleNamespace(app=app, screen=screen, sel=sel, keys=keys, clip=clip,
                              engine=engine, buffer=buffer)
    finally:
        for closer in (sel, keys, clip):
            if closer is not None:
                closer.close()
        for proc in reversed(procs):
            _stop(proc)
        if saved_display is None:
            os.environ.pop("DISPLAY", None)
        else:
            os.environ["DISPLAY"] = saved_display


def _select_all(session) -> None:
    session.screen.chord("Control_L", "a")
    assert _wait(lambda: session.sel.primary_text(1000) == session.buffer(), 3.0)


def test_primary_selection_is_read_and_its_owner_is_the_active_window(session):
    _select_all(session)
    assert session.sel.primary_text(1000) == TEXT
    assert session.sel.primary_text(5) is None  # larger than the limit: not read
    owner, active = session.sel.owner(), session.sel.active()
    assert owner is not None and active is not None
    assert owner.xid == active.xid and active.pid == session.app.pid
    assert active.wm_class.casefold() == "gtk_app" and active.title == "glance-test-app"
    x, y = session.sel.pointer()
    assert 0 <= x < 1280 and 0 <= y < 800


def test_uncaptured_app_paste_over(session):
    _select_all(session)
    active = session.sel.active()
    assert session.keys.focus_target()[0] == active.xid
    before = session.clip.save()
    assert before is not None, "the private session's clipboard must be restorable"
    result = session.engine.paste_unverified(REWRITE, active.xid)
    assert result.status == "unverified", result
    assert _wait(lambda: session.buffer() == REWRITE, 3.0), session.buffer()
    after = session.clip.save()
    assert after is not None and after.payload == before.payload  # the clipboard is as it was
    # One Ctrl+Z in the app undoes the paste, as the hint says; Glance's own revert does not.
    assert session.engine.revert_last().status == "stale"
    session.screen.chord("Control_L", "z")
    assert _wait(lambda: session.buffer() == TEXT, 3.0), session.buffer()


def test_paste_into_another_window_than_the_selection_is_refused(session):
    _select_all(session)
    active = session.sel.active()
    result = session.engine.paste_unverified(REWRITE, active.xid + 1)
    assert result.status == "stale"
    assert session.buffer() == TEXT


def _helper(d, pid=None, toplevel=False):
    from Xlib import X, Xatom

    root = d.screen().root
    w = root.create_window(0, 0, 50, 50, 0, d.screen().root_depth)
    if pid is not None:
        w.change_property(d.intern_atom("_NET_WM_PID"), Xatom.CARDINAL, 32, [pid])
    if toplevel:
        w.set_wm_class("other", "Other")
        w.set_wm_name("second window")
        w.map()
    w.set_selection_owner(Xatom.PRIMARY, X.CurrentTime)
    d.sync()
    return w


def test_helper_window_owner_without_a_pid_is_unknown(session):
    """A bare window without WM_CLASS or pid owns PRIMARY: never attributed to anyone."""
    from Xlib import display

    d = display.Display(os.environ["DISPLAY"])
    helper = _helper(d)
    try:
        assert session.sel.owner() is None
    finally:
        helper.destroy()
        d.sync()
        d.close()


def test_helper_window_resolves_only_when_its_process_has_one_top_level(session):
    from Xlib import X, Xatom, display

    d = display.Display(os.environ["DISPLAY"])
    helper = _helper(d, pid=session.app.pid)
    second = None
    try:
        owner = session.sel.owner()
        assert owner is not None and owner.pid == session.app.pid
        # A second top-level window of the same process: now it cannot be told which one
        # the selection came from (the stale-selection-in-a-private-window case).
        second = _helper(d, pid=session.app.pid, toplevel=True)
        helper.set_selection_owner(Xatom.PRIMARY, X.CurrentTime)  # `second` took it
        d.sync()
        assert _wait(lambda: sum(session.sel._window_pid(w) == session.app.pid
                                 for w in session.sel._client_windows()) == 2, 3.0)
        assert session.sel.owner() is None
    finally:
        for w in (second, helper):
            if w is not None:
                w.destroy()
        d.sync()
        d.close()
