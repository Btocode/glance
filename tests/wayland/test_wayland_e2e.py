"""Glance on GNOME Wayland, end to end, in a private headless GNOME Shell.

Run on a machine with GNOME Shell 46-50 (never touches the user's session):

    GLANCE_WAYLAND_TEST=1 tests/wayland/private-session.sh ~/glance-wltest \\
        .venv/bin/python -m pytest -m wayland tests/wayland -p no:cacheprovider

The tests share one shell, one GTK test app and one glanced (fake AI backend) and run in
file order.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from tests.wayland.harness import (
    TYPING,
    Glance,
    GtkApp,
    Shell,
    gsettings,
    private_root,
    wait_for,
)

pytestmark = [
    pytest.mark.wayland,
    pytest.mark.skipif(os.environ.get("GLANCE_WAYLAND_TEST") != "1",
                       reason="set GLANCE_WAYLAND_TEST=1 (and run in private-session.sh)"),
    pytest.mark.skipif(private_root() is None,
                       reason="only inside tests/wayland/private-session.sh"),
    pytest.mark.skipif(shutil.which("gnome-shell") is None, reason="no gnome-shell"),
]

TEXT = "We needs a plan for this week and it is fine."
FIXED = "We need a plan for this week and it is fine."
SHOTS = os.environ.get("GLANCE_WAYLAND_SHOTS")  # a directory for screenshots, optional


@pytest.fixture(scope="module")
def desk():
    root = private_root()
    assert root is not None
    shell = Shell(root).start()
    app = glance = None
    try:
        app = GtkApp(shell, TEXT, "--undo")
        glance = Glance(shell, root, TYPING, name="glanced-e2e")
        assert wait_for(lambda: glance.status().get("shell_extension") == "running", 20)
        yield shell, app, glance, root
    finally:
        for part in (glance, app):
            if part is not None:
                part.stop()
        shell.stop()


def shot(shell: Shell, name: str) -> None:
    """With $GLANCE_WAYLAND_SHOTS: the screen with the private session switched to light
    (wayland-<name>-light.png) and to dark (wayland-<name>-dark.png), then set back."""
    if not SHOTS:
        return
    Path(SHOTS).mkdir(parents=True, exist_ok=True)
    for theme, scheme, gtk in (("light", "prefer-light", "Adwaita"),
                               ("dark", "prefer-dark", "Adwaita-dark")):
        gsettings("set", "org.gnome.desktop.interface", "color-scheme", scheme)
        gsettings("set", "org.gnome.desktop.interface", "gtk-theme", gtk)
        time.sleep(1.0)  # glanced follows it and sends the scene again
        shell.screenshot(Path(SHOTS) / f"wayland-{name}-{theme}.png")
    gsettings("set", "org.gnome.desktop.interface", "color-scheme", "default")
    gsettings("reset", "org.gnome.desktop.interface", "gtk-theme")
    time.sleep(1.0)


def target_still_focused(shell: Shell, app: GtkApp) -> bool:
    state = shell.call("FocusedWindow")
    focus = state.get("focus") or {}
    return focus.get("pid") == app.proc.pid and not state.get("shell_has_keyboard")


def test_icon_appears_for_the_focused_field(desk):
    shell, _app, glance, _root = desk
    ov = wait_for(lambda: (o := shell.overlay()).get("status")
                  and o["status"]["state"] == "ready" and o, 30)
    assert ov, shell.overlay()
    assert ov["status"]["badge"] == "1"
    x, y, _w, _h = ov["status"]["rect"]
    frame = shell.focused()["frame"]
    # at the right end of the caret line, inside the window
    assert frame[0] < x < frame[0] + frame[2] and frame[1] < y < frame[1] + frame[3]
    assert glance.status()["field"]["wm_class"] == "gtk_app.py"
    shot(shell, "icon")


def test_strip_appears_on_icon_click_and_focus_stays(desk):
    shell, app, glance, _root = desk
    assert shell.call("TestClick", {"target": "status"})["ok"]
    # (a strip shown a moment ago may have no text and no position yet)
    ov = wait_for(lambda: (o := shell.overlay()).get("strip") and o["strip"]["text"] == FIXED
                  and None not in o["strip"]["rect"] and o, 10)
    assert ov, shell.overlay()
    _sx, sy, _sw, _sh = ov["strip"]["rect"]
    assert sy > ov["status"]["rect"][1]  # under the caret line
    assert target_still_focused(shell, app)
    assert glance.status()["field"] is not None  # the field did not blur
    shot(shell, "strip")


def test_alt_enter_applies_the_corrected_sentence(desk):
    shell, app, glance, _root = desk
    assert wait_for(lambda: glance.request("test_overlay").get("enter_grabbed"), 5)
    # the user presses Alt+Enter (through the shell's own virtual keyboard)
    done = len(glance.applies())
    assert shell.call("Chord", {"keys": ["alt", "Return"]})["ok"]
    assert wait_for(lambda: len(glance.applies()) > done, 15, 0.1)
    assert glance.applies()[-1] == "status=verified"
    assert app.buffer() == FIXED
    assert target_still_focused(shell, app)
    # nothing left to fix: the strip goes, the icon goes back to idle
    assert wait_for(lambda: not shell.overlay().get("strip"), 10)


def test_overlay_follows_a_window_move_and_resize(desk):
    shell, app, _glance, _root = desk
    ov = wait_for(lambda: (o := shell.overlay()).get("status") and o, 10)
    before = ov["status"]["rect"]
    frame = shell.focused()["frame"]
    assert shell.call("TestMoveWindow", {"x": frame[0] - 150, "y": frame[1] + 60,
                                         "w": frame[2], "h": frame[3]})["ok"]
    moved = wait_for(lambda: (o := shell.overlay()).get("status")
                     and o["status"]["rect"][0] == before[0] - 150 and o, 5)
    assert moved, (before, shell.overlay())
    assert moved["status"]["rect"][1] == before[1] + 60
    # a resize: hidden while it happens, placed again from the fresh field rectangles
    frame = shell.focused()["frame"]
    assert shell.call("TestMoveWindow", {"x": frame[0], "y": frame[1], "w": frame[2] + 200,
                                         "h": frame[3]})["ok"]
    after = wait_for(lambda: (o := shell.overlay()).get("status")
                     and o["status"]["rect"][0] > moved["status"]["rect"][0] + 100 and o, 5)
    assert after, shell.overlay()
    assert target_still_focused(shell, app)


def test_select_anything_reads_the_selection(desk):
    shell, app, glance, _root = desk
    assert shell.call("Chord", {"keys": ["ctrl", "a"]})["ok"]  # select all in the field
    assert wait_for(lambda: shell.call("ReadPrimary", {"max_bytes": 1000}).get("text"), 5)
    assert shell.call("Chord", {"keys": ["super", "alt", "a"]})["ok"]
    ov = wait_for(lambda: (o := shell.overlay()).get("panel") and o, 10)
    assert ov and ov["panel"]["mode"] == "selection", shell.overlay()
    assert glance.status()["selection_open"]
    assert target_still_focused(shell, app)
    shot(shell, "selection")
    assert shell.call("TestClick", {"target": "close"})["ok"]
    assert wait_for(lambda: not shell.overlay().get("panel"), 5)


REWRITE = "Thanks so much, will do!"  # FakeAiBackend.REWRITE


def test_drawer_tone_card_applies_the_rewrite(desk):
    shell, app, glance, _root = desk
    assert shell.call("Chord", {"keys": ["End"]})["ok"]  # drop select-anything's selection
    assert shell.call("Chord", {"keys": ["super", "alt", "space"]})["ok"]  # the panel hotkey
    ov = wait_for(lambda: (o := shell.overlay()).get("panel")
                  and any(n.startswith("card:") for n in o["named"]) and o, 10)
    assert ov and ov["panel"]["mode"] == "field", shell.overlay()
    assert "rewrite-as" in ov["named"]
    shot(shell, "drawer")
    card = min(n for n in ov["named"] if n.startswith("card:"))
    done = len(glance.applies())
    assert shell.call("TestClick", {"target": card})["ok"]
    assert wait_for(lambda: len(glance.applies()) > done, 15, 0.1)
    assert glance.applies()[-1] == "status=verified"

    assert wait_for(lambda: app.buffer() == REWRITE, 15, 0.2), app.buffer()
    assert target_still_focused(shell, app)


def test_rewrite_as_takes_the_keyboard_and_gives_it_back(desk):
    shell, app, glance, _root = desk
    assert shell.call("Chord", {"keys": ["ctrl", "a"]})["ok"]
    assert shell.call("Chord", {"keys": ["BackSpace"]})["ok"]
    assert shell.call("TypeText", {"text": "We needs a plan."})["ok"]
    assert wait_for(lambda: app.buffer() == "We needs a plan.", 5, 0.1)
    if not shell.overlay().get("panel"):
        assert shell.call("Chord", {"keys": ["super", "alt", "space"]})["ok"]
    assert wait_for(lambda: "rewrite-as" in shell.overlay()["named"], 10)
    assert shell.call("TestClick", {"target": "rewrite-as"})["ok"]
    if not wait_for(lambda: shell.overlay()["ask"], 3):
        # seen once in ~10 runs: the drawer was redrawn under the click; click again
        assert shell.call("TestClick", {"target": "rewrite-as"})["ok"]
    assert wait_for(lambda: shell.overlay()["ask"], 5)
    state = shell.call("FocusedWindow")
    assert state["shell_has_keyboard"]  # the entry has the keys now (a modal grab)
    assert state["focus"]["pid"] == app.proc.pid  # ...and the app is still the focused window
    shot(shell, "rewrite-as")
    done = len(glance.applies())
    assert shell.call("TestAskText", {"text": "make it short", "submit": True})["ok"]

    assert wait_for(lambda: target_still_focused(shell, app), 5)
    assert wait_for(lambda: len(glance.applies()) > done, 15, 0.1)
    assert glance.applies()[-1] == "status=verified"
    assert app.buffer() == REWRITE


def test_autocomplete_ghost_and_tab(desk):
    shell, app, glance, _root = desk
    assert shell.call("Chord", {"keys": ["ctrl", "a"]})["ok"]
    assert shell.call("Chord", {"keys": ["BackSpace"]})["ok"]
    assert shell.call("TypeText", {"text": "Let us meet and talk about it"})["ok"]
    assert wait_for(lambda: app.buffer() == "Let us meet and talk about it", 5, 0.1)
    if shell.overlay().get("panel"):
        assert shell.call("Chord", {"keys": ["Escape"]})["ok"]
    time.sleep(0.5)
    assert shell.call("Chord", {"keys": ["super", "alt", "Return"]})["ok"]
    ov = wait_for(lambda: (o := shell.overlay()).get("ghost") and o, 10)
    assert ov, shell.overlay()
    shot(shell, "ghost")
    done = len(glance.applies())
    assert shell.call("Chord", {"keys": ["Tab"]})["ok"]
    assert wait_for(lambda: len(glance.applies()) > done, 15, 0.1)
    assert glance.applies()[-1] == "status=verified"
    assert app.buffer() == "Let us meet and talk about it tomorrow morning."
    assert target_still_focused(shell, app)


def test_nothing_typed_reaches_the_logs(desk):
    _shell, _app, glance, root = desk
    time.sleep(0.5)
    for log in (glance.log, root / "shell.log"):
        text = log.read_text(errors="replace")
        for words in ("needs a plan", "need a plan", "this week and it is fine", "will do",
                      "talk about it", "tomorrow morning", "make it short"):
            assert words not in text, f"{words!r} in {log.name}"
