"""Ctrl+G end to end: `glance-edit` as Claude Code would run it, the GTK edit window, keys.

Everything runs on a private Xvfb server with a minimal window manager (`mini_wm.py`) and a
stand-in daemon (`edit_host.py`) that serves only `edit_session`, with the daemon's test AI
backend. The display the user works on is never touched. Tests share one session and run
in file order; the last one kills the host.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.desktop.test_capture_gtk import Lines
from tests.desktop.test_panel_tone import Screen, _stop, _wait

HERE = Path(__file__).parent
TITLE = "Glance — edit prompt"
HARPER = Path.home() / ".local/share/glance/bin/harper-ls"

pytestmark = [pytest.mark.xvfb,
              pytest.mark.skipif(shutil.which("Xvfb") is None, reason="Xvfb is not installed")]


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    home = tmp_path_factory.mktemp("edit")
    dirs = {name: home / name for name in ("rt", "cfg", "data", "prompts")}
    for d in dirs.values():
        d.mkdir(mode=0o700)
    if HARPER.exists():
        (dirs["data"] / "glance" / "bin").mkdir(parents=True)
        (dirs["data"] / "glance" / "bin" / "harper-ls").symlink_to(HARPER)
    editor = home / "editor.sh"
    editor.write_text('#!/bin/sh\nprintf X >> "$1"\n')
    editor.chmod(0o700)
    procs: list[subprocess.Popen] = []
    log = (home / "host.stderr").open("w")
    try:
        read_fd, write_fd = os.pipe()
        procs.append(subprocess.Popen(
            ["Xvfb", "-displayfd", str(write_fd), "-screen", "0", "1280x800x24", "-nolisten",
             "tcp"], pass_fds=[write_fd], stderr=subprocess.DEVNULL))
        os.close(write_fd)
        with os.fdopen(read_fd) as f:
            number = f.readline().strip()
        assert number, "Xvfb did not start"
        env = {**os.environ, "DISPLAY": f":{number}", "GDK_BACKEND": "x11", "NO_AT_BRIDGE": "1",
               "XDG_RUNTIME_DIR": str(dirs["rt"]), "XDG_CONFIG_HOME": str(dirs["cfg"]),
               "XDG_DATA_HOME": str(dirs["data"]), "GLANCE_STRICT_LOG": "1"}
        for name in ("WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "AT_SPI_BUS_ADDRESS",
                     "CLAUDE_CODE_ENTRYPOINT", "WINDOWID", "VISUAL", "EDITOR"):
            env.pop(name, None)
        wm = subprocess.Popen([sys.executable, str(HERE / "mini_wm.py")], env=env,
                              stdout=subprocess.PIPE, text=True)
        procs.append(wm)
        assert Lines(wm).wait_for(lambda line: line == "READY", timeout=10), "no window manager"
        host = subprocess.Popen([sys.executable, str(HERE / "edit_host.py")], env=env,
                                stdout=subprocess.PIPE, stderr=log, text=True)
        procs.append(host)
        assert Lines(host).wait_for(lambda line: line == "READY", timeout=20), \
            (home / "host.stderr").read_text()[-2000:]
        screen = Screen(env["DISPLAY"])
        shim_env = {**env, "GLANCE_TEST": "1", "GLANCE_TEST_FORCE_CLAUDE": "1",
                    "GLANCE_REAL_EDITOR": str(editor)}

        def prompt(text: str) -> Path:
            f = dirs["prompts"] / f"claude-prompt-{uuid.uuid4()}.md"
            f.write_text(text)
            return f

        def edit(f: Path) -> subprocess.Popen:
            p = subprocess.Popen([sys.executable, "-m", "glance.edit_shim.main", str(f)],
                                 env=shim_env, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, text=True)
            procs.append(p)
            return p

        def window_up() -> bool:
            return _wait(lambda: active_title(screen) == (host.pid, TITLE), 10)

        yield SimpleNamespace(screen=screen, prompt=prompt, edit=edit, host=host,
                              window_up=window_up, env=env, log=home / "host.stderr")
    finally:
        for proc in reversed(procs):
            _stop(proc)
        log.close()


def active_title(s: Screen) -> tuple[int, str]:
    """(pid, _NET_WM_NAME) of the active window: the title is not Latin-1, so WM_NAME
    does not carry it."""
    from Xlib import X, error

    atom = s.d.intern_atom
    try:
        prop = s.root.get_full_property(atom("_NET_ACTIVE_WINDOW"), X.AnyPropertyType)
        if not prop or not prop.value[0]:
            return 0, ""
        win = s.d.create_resource_object("window", int(prop.value[0]))
        pid = win.get_full_property(atom("_NET_WM_PID"), X.AnyPropertyType)
        name = win.get_full_property(atom("_NET_WM_NAME"), atom("UTF8_STRING"))
        value = name.value if name else ""
        title = value.decode() if isinstance(value, bytes) else str(value)
        return (int(pid.value[0]) if pid else 0), title
    except error.XError:
        return 0, ""


def _exit(p: subprocess.Popen, timeout: float = 5) -> int:
    try:
        return p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pytest.fail("glance-edit did not exit")


def test_edit_window_apply_round_trip(session):
    f = session.prompt("fix teh bug\n")
    p = session.edit(f)
    assert session.window_up(), (active_title(session.screen), session.log.read_text()[-2000:])
    session.screen.type(" now")
    session.screen.chord("Control_L", "Return")
    assert _exit(p) == 0, p.stderr.read() if p.stderr else ""
    assert f.read_text() == "fix teh bug now\n"


def test_edit_window_escape_cancels(session):
    f = session.prompt("fix teh bug")
    p = session.edit(f)
    assert session.window_up()
    session.screen.type(" now")
    session.screen.chord("Escape")
    assert _exit(p) == 0
    assert f.read_text() == "fix teh bug"


def test_open_in_editor_chains(session):
    f = session.prompt("fix teh bug")
    p = session.edit(f)
    assert session.window_up()
    session.screen.type(" now")
    session.screen.chord("Control_L", "e")
    assert _exit(p) == 0
    assert f.read_text() == "fix teh bug nowX"


def test_second_request_is_cancelled_and_first_stays(session):
    first, second = session.prompt("one"), session.prompt("two")
    p1 = session.edit(first)
    assert session.window_up()
    p2 = session.edit(second)
    assert _exit(p2) == 0 and second.read_text() == "two"
    assert session.window_up()  # still the first one's window, in front
    session.screen.chord("Control_L", "Return")
    assert _exit(p1) == 0 and first.read_text() == "one"


def test_closing_the_window_cancels(session):
    from Xlib import X
    from Xlib.protocol import event

    f = session.prompt("fix teh bug")
    p = session.edit(f)
    assert session.window_up()
    s = session.screen
    prop = s.root.get_full_property(s.d.intern_atom("_NET_ACTIVE_WINDOW"), X.AnyPropertyType)
    win = s.d.create_resource_object("window", int(prop.value[0]))
    msg = event.ClientMessage(window=win, client_type=s.d.intern_atom("WM_PROTOCOLS"),
                              data=(32, [s.d.intern_atom("WM_DELETE_WINDOW"), X.CurrentTime,
                                         0, 0, 0]))
    win.send_event(msg)
    s.d.sync()
    assert _exit(p) == 0 and f.read_text() == "fix teh bug"


@pytest.fixture(scope="module")
def probe(session):
    import json

    done = subprocess.run([sys.executable, str(HERE / "edit_window_probe.py")],
                          env={**session.env, "PYTHONPATH": str(HERE.parent.parent)},
                          capture_output=True, text=True, timeout=60,
                          check=False)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_window_is_centred_over_the_terminal_and_titled(probe):
    assert probe["centre"] == [600, 400] and probe["title"] == TITLE
    assert probe["keep_above_hint"] == "normal"


def test_window_offers_sharpen_first_and_underlines_issues(probe):
    assert probe["chips"] == ["Sharpen prompt", "Add structure", "Shorter", "Rewrite as…"]
    assert probe["underlined"] == [False, True, True, False]  # "teh" is 4..7
    assert probe["issues_label"] == "1 issue"
    assert probe["fix_labels"] == ["the", "tech"]


def test_window_preview_use_and_undo(probe):
    assert probe["preview_visible_before"] is False
    assert probe["preview_visible"] and probe["use_sensitive"]
    assert probe["preview_text"] == "fix tehFix the bug in [which file?]"  # del, ins, eq
    assert probe["after_use"] == "Fix the bug in [which file?]"
    assert probe["preview_visible_after_use"] is False
    assert probe["after_undo"] == "fix teh bug"


def test_window_issue_fix_typing_and_custom(probe):
    assert probe["after_fix"] == "fix tech bug" and probe["issues_after_fix"] == "No issues"
    assert probe["session_follows_typing"] == "fix tech bug now"
    assert probe["custom_row_visible"] and probe["custom_call"] == ["custom", "as a list"]
    assert probe["done"] == [["cancel", ""]]


def test_daemon_dying_mid_session_opens_the_real_editor(session):
    f = session.prompt("fix teh bug")
    p = session.edit(f)
    assert session.window_up()
    session.host.terminate()
    assert _exit(p) == 0
    assert f.read_text() == "fix teh bugX"  # the real editor ran on the untouched prompt
