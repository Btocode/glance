"""Real Wayland apps in the private headless GNOME Shell: a GTK 4 editor and Chrome.

Each is skipped when it is not installed. Same rules as test_wayland_e2e.py: run inside
tests/wayland/private-session.sh with GLANCE_WAYLAND_TEST=1.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tests.wayland.harness import TYPING, Glance, Shell, private_root, wait_for
from tests.wayland.test_wayland_e2e import FIXED, TEXT, pytestmark, shot  # noqa: F401


@pytest.fixture(scope="module")
def desk():
    root = private_root()
    assert root is not None
    shell = Shell(root).start()
    glance = None
    try:
        glance = Glance(shell, root, TYPING, name="glanced-apps")
        assert wait_for(lambda: glance.status().get("shell_extension") == "running", 20)
        yield shell, glance, root
    finally:
        if glance is not None:
            glance.stop()
        shell.stop()


def run_app(shell: Shell, argv: list[str], log: Path) -> subprocess.Popen:
    proc = subprocess.Popen(argv, env=shell.app_env(), stdout=subprocess.DEVNULL,
                            stderr=log.open("wb"))
    if not wait_for(lambda: (shell.focused() or {}).get("pid") == proc.pid, 40, 0.2):
        proc.terminate()
        pytest.fail(f"{argv[0]} did not get a focused window")
    time.sleep(2)  # its accessibility tree comes up a moment after the window
    return proc


def fix_in_place(shell: Shell, glance: Glance, name: str) -> None:
    """Type the sentence, wait for the icon, open the strip, Alt+Enter, check the result."""
    assert shell.call("TypeText", {"text": TEXT})["ok"]
    ov = wait_for(lambda: (o := shell.overlay()).get("status")
                  and o["status"]["state"] == "ready" and o, 30)
    assert ov, (shell.overlay(), glance.status().get("field"))
    frame = shell.focused()["frame"]
    x, y, _w, _h = ov["status"]["rect"]
    # on the caret line: inside the window, or just right of a field that fills it
    assert frame[0] <= x <= frame[0] + frame[2] + 30 and frame[1] <= y <= frame[1] + frame[3]
    assert shell.call("TestClick", {"target": "status"})["ok"]
    ov = wait_for(lambda: (o := shell.overlay()).get("strip") and o["strip"]["text"] == FIXED
                  and o, 15)
    assert ov, shell.overlay()
    # the strip is under the caret line, so under the icon that sits on it
    assert ov["strip"]["rect"][1] > ov["status"]["rect"][1]
    shot(shell, f"{name}-strip")
    done = len(glance.applies())
    assert shell.call("Chord", {"keys": ["alt", "Return"]})["ok"]
    assert wait_for(lambda: len(glance.applies()) > done, 15, 0.1)
    assert glance.applies()[-1] == "status=verified"
    state = shell.call("FocusedWindow")
    assert not state["shell_has_keyboard"]


@pytest.mark.skipif(shutil.which("gnome-text-editor") is None,
                    reason="gnome-text-editor is not installed")
def test_gtk4_text_editor(desk):
    shell, glance, root = desk
    for d in ("gnome-text-editor", "org.gnome.TextEditor"):  # no drafts from an earlier run
        shutil.rmtree(Path(os.environ["XDG_DATA_HOME"]) / d, ignore_errors=True)
        shutil.rmtree(Path(os.environ["XDG_STATE_HOME"]) / d, ignore_errors=True)
    app = run_app(shell, ["gnome-text-editor", "--standalone", "--ignore-session"],
                  root / "gnome-text-editor.log")
    try:
        assert shell.focused()["wm_class"] == "org.gnome.TextEditor"
        fix_in_place(shell, glance, "gtk4")
    finally:
        app.terminate()
        app.wait(10)


CHROME = shutil.which("google-chrome") or shutil.which("chromium")


@pytest.mark.skipif(CHROME is None, reason="no Chrome or Chromium")
def test_chrome_textarea(desk):
    shell, glance, root = desk
    page = root / "textarea.html"
    page.write_text("<html><body style='margin:40px'>"
                    "<textarea autofocus rows=6 cols=60></textarea></body></html>")
    profile = root / "chrome-profile"
    shutil.rmtree(profile, ignore_errors=True)
    app = run_app(shell, [CHROME, "--ozone-platform=wayland", "--force-renderer-accessibility",
                          f"--user-data-dir={profile}", "--no-first-run",
                          "--no-default-browser-check", "--disable-gpu",
                          "--password-store=basic", "--new-window", f"file://{page}"],
                  root / "chrome.log")
    try:
        fix_in_place(shell, glance, "chrome")
    finally:
        app.terminate()
        app.wait(15)
