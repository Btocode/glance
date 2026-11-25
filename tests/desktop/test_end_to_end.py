"""glanced + glance-capture + harper-ls against a real GTK window. Run in the GNOME session:

    .venv/bin/pytest -m desktop tests/desktop/test_end_to_end.py -v

Before running: stop your own glanced (`glance status` must say it is not running) - the
test daemon grabs the same hotkeys - and keep your hands off the keyboard: the test presses
Super+Alt+F and Super+Alt+Z with XTEST while its own window is the active one, and the
fix is typed into that window. The daemon runs with its runtime, config and data
directories in a temp dir; nothing of yours is read or changed.

The tests share one session and run in file order.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.desktop.test_capture_gtk import Desktop, Lines, _stop
from tests.desktop.test_writeback_gtk import _pump

pytestmark = pytest.mark.desktop

APP = Path(__file__).with_name("gtk_app.py")
TEXT = "Teh quick brown fox jumps over the lazy dog."
HARPER = Path.home() / ".local/share/glance/bin/harper-ls"


def _wait(pred, timeout: float, step: float = 0.05):
    deadline = time.monotonic() + timeout
    while True:
        value = pred()
        if value or time.monotonic() > deadline:
            return value
        time.sleep(step)


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    if not os.environ.get("DISPLAY"):
        pytest.skip("no X display")
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        pytest.skip("DBUS_SESSION_BUS_ADDRESS is not set: AT-SPI is unreachable once "
                    "XDG_RUNTIME_DIR points elsewhere")
    if not HARPER.exists():
        pytest.skip("harper-ls is not installed: run scripts/fetch-harper.sh")
    home = tmp_path_factory.mktemp("glance-e2e")
    dirs = {name: home / name for name in ("runtime", "config", "data")}
    for d in dirs.values():
        d.mkdir(mode=0o700)
    (dirs["data"] / "glance" / "bin").mkdir(parents=True)
    (dirs["data"] / "glance" / "bin" / "harper-ls").symlink_to(HARPER)
    log_path = tmp_path_factory.mktemp("glance-e2e-log") / "glanced.stderr"
    env = {**os.environ, "XDG_RUNTIME_DIR": str(dirs["runtime"]),
           "XDG_CONFIG_HOME": str(dirs["config"]), "XDG_DATA_HOME": str(dirs["data"]),
           "GLANCE_STRICT_LOG": "0"}

    def glance(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "glance.cli", *args], env=env,
                              capture_output=True, text=True, timeout=10, check=False)

    def status() -> dict:
        done = glance("status", "--json")
        return json.loads(done.stdout) if done.returncode == 0 else {}

    assert glance("status").returncode == 3, "a glanced already answers in the temp dir"
    app = subprocess.Popen([sys.executable, str(APP), "--text", TEXT, "--undo"],
                           stdout=subprocess.PIPE, text=True)
    daemon = None
    log = log_path.open("w")
    try:
        out = Lines(app)
        assert out.wait_for(lambda line: line == "READY", timeout=10), "gtk_app not ready"
        daemon = subprocess.Popen([sys.executable, "-m", "glance.daemon.main"], env=env,
                                  stderr=log)
        assert _wait(lambda: status().get("capture_connected"), 10), "capture never connected"
        st = status()
        assert st["hotkeys_unavailable"] is None, (
            f"hotkeys taken by another program: {st['hotkeys_unavailable']}")
        desktop = Desktop()
        _pump(0.3)
        assert desktop.activate(app.pid), "could not give the test window focus"

        def buffer() -> str:
            seen = len(out.lines)
            app.send_signal(signal.SIGUSR1)
            line = out.wait_for(lambda text: text, timeout=2.0, start=seen)
            assert line is not None, "gtk_app did not print its buffer"
            return json.loads(line)

        def hotkey(key: str) -> None:
            from Xlib import XK, X
            from Xlib.ext import xtest

            assert desktop.active_pid() == app.pid, "test window is not active; no key sent"
            dpy = desktop._dpy
            codes = [dpy.keysym_to_keycode(XK.string_to_keysym(k))
                     for k in ("Super_L", "Alt_L", key)]
            for code in codes:
                xtest.fake_input(dpy, X.KeyPress, code)
            for code in reversed(codes):
                xtest.fake_input(dpy, X.KeyRelease, code)
            dpy.sync()

        yield SimpleNamespace(app=app, daemon=daemon, status=status, glance=glance,
                              buffer=buffer, hotkey=hotkey, dirs=dirs, log_path=log_path)
    finally:
        if daemon is not None:
            _stop(daemon)
        log.close()
        _stop(app)


def test_fix_all_hotkey_fixes_text(session):
    assert _wait(lambda: session.status().get("local_issues", 0) >= 1, 5), session.status()
    assert session.buffer().startswith("Teh quick")
    session.hotkey("f")
    assert _wait(lambda: session.buffer().startswith("The quick"), 2.0), session.buffer()


def test_revert_hotkey_restores(session):
    assert session.buffer().startswith("The quick"), "needs the fix from the test before"
    session.hotkey("z")
    assert _wait(lambda: session.buffer().startswith("Teh quick"), 2.0), session.buffer()


def test_pause_cli(session):
    assert session.glance("pause", "1m").returncode == 0
    st = session.status()
    assert st["paused"] is True and 0 < st["pause_remaining_s"] <= 60
    assert "paused: true" in session.glance("status").stdout.splitlines()
    assert session.glance("resume").returncode == 0
    assert session.status()["paused"] is False


def test_second_daemon_refuses_to_start(session):
    env = {**os.environ, "XDG_RUNTIME_DIR": str(session.dirs["runtime"]),
           "XDG_CONFIG_HOME": str(session.dirs["config"]),
           "XDG_DATA_HOME": str(session.dirs["data"])}
    done = subprocess.run([sys.executable, "-m", "glance.daemon.main"], env=env,
                          capture_output=True, text=True, timeout=10, check=False)
    assert done.returncode == 1 and "already running" in done.stderr


def test_capture_killed_restarts_within_2s(session):
    assert session.status()["capture_connected"] is True
    # Only the capture started by this test's daemon: its parent is the daemon.
    done = subprocess.run(["pgrep", "-P", str(session.daemon.pid), "-f", "glance.capture.main"],
                          capture_output=True, text=True, check=False)
    pids = [int(p) for p in done.stdout.split()]
    assert len(pids) == 1, pids
    os.kill(pids[0], signal.SIGTERM)
    assert _wait(lambda: not session.status().get("capture_connected"), 2.0, 0.02)
    t0 = time.monotonic()
    assert _wait(lambda: session.status().get("capture_connected"), 4.0, 0.02)
    assert time.monotonic() - t0 < 2.0


def test_harper_killed_restarts(session):
    done = subprocess.run(["pgrep", "-P", str(session.daemon.pid), "-f", "harper-ls"],
                          capture_output=True, text=True, check=False)
    pids = [int(p) for p in done.stdout.split()]
    assert len(pids) == 1, pids
    os.kill(pids[0], signal.SIGTERM)
    assert _wait(lambda: session.status().get("harper_alive") is False, 2.0, 0.02)
    assert _wait(lambda: session.status().get("harper_alive") is True, 4.0)


def test_nothing_typed_on_disk(session):
    session.log_path.read_text()  # exists
    paths = [str(d) for d in session.dirs.values()] + [str(session.log_path)]
    for needle in ("quick brown", "lazy dog"):
        done = subprocess.run(["grep", "-r", "-l", needle, *paths], capture_output=True,
                              text=True, check=False)
        assert done.returncode == 1 and done.stdout == "", done.stdout
