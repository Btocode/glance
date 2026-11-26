"""`[general] check = "shortcut"` (the default) on GNOME Wayland, in the private headless shell.

Typing checks nothing; a click on the status icon, the check keybinding (Super+Alt+G) or the
hover menu's "Check spelling & grammar" checks the field and opens the strip, the app keeping
the keyboard. Killing glanced while the strip is up gives every key back.

Same rules as test_wayland_e2e.py: run inside tests/wayland/private-session.sh with
GLANCE_WAYLAND_TEST=1. The tests share one shell, app and glanced and run in file order; the
last one kills glanced.
"""
from __future__ import annotations

import time

import pytest

from tests.wayland.harness import Glance, GtkApp, Shell, private_root, wait_for
from tests.wayland.test_wayland_e2e import (  # noqa: F401
    FIXED,
    TEXT,
    pytestmark,
    shot,
    target_still_focused,
)


@pytest.fixture(scope="module")
def desk():
    root = private_root()
    assert root is not None
    shell = Shell(root).start()
    app = glance = None
    try:
        app = GtkApp(shell, TEXT, "--undo")
        glance = Glance(shell, root, None, name="glanced-shortcut")  # no config: "shortcut"
        assert wait_for(lambda: glance.status().get("shell_extension") == "running", 20)
        yield shell, app, glance
    finally:
        for part in (glance, app):
            if part is not None:
                part.stop()
        shell.stop()


def icon(shell: Shell, state: str | None = None, timeout_s: float = 30) -> dict:
    ov = wait_for(lambda: (o := shell.overlay()).get("status")
                  and (state is None or o["status"]["state"] == state) and o, timeout_s)
    assert ov, shell.overlay()
    return ov


def placed(piece: dict | None) -> bool:
    """On screen and allocated (a piece shown a moment ago has no position yet)."""
    return bool(piece) and all(isinstance(v, int) for v in piece["rect"])


def strip_with_fix(shell: Shell, glance: Glance) -> dict:
    ov = wait_for(lambda: placed((o := shell.overlay()).get("strip"))
                  and o["strip"]["text"] == FIXED and placed(o["status"]) and o, 15)
    assert ov, (shell.overlay(), glance.request("test_overlay"))
    return ov


def close_strip(shell: Shell) -> None:
    assert shell.call("Chord", {"keys": ["Escape"]})["ok"]  # Esc is Glance's while it is up
    assert wait_for(lambda: not shell.overlay().get("strip"), 5), shell.overlay()


def grabs(shell: Shell) -> set[str]:
    return {g["name"] for g in shell.call("TestGrabs")["grabs"]}


def test_icon_click_checks_and_opens_the_strip(desk):
    shell, app, glance = desk
    icon(shell, "idle")  # quietly ready: nothing is checked by itself
    assert "check" in grabs(shell)  # the check keybinding is the extension's
    assert glance.request("test_state")["issues"] == []
    assert shell.call("TestClick", {"target": "status"})["ok"]
    ov = strip_with_fix(shell, glance)
    assert ov["strip"]["rect"][1] > ov["status"]["rect"][1]  # under the caret line
    assert ov["status"]["state"] == "ready" and ov["status"]["badge"] == "1"
    assert target_still_focused(shell, app)
    assert glance.status()["field"] is not None  # the field did not blur
    shot(shell, "shortcut-click")
    close_strip(shell)
    assert target_still_focused(shell, app)


def test_check_keybinding_opens_the_strip(desk):
    shell, app, glance = desk
    icon(shell)
    # the user presses Super+Alt+G (through the shell's own virtual keyboard)
    assert shell.call("Chord", {"keys": ["super", "alt", "g"]})["ok"]
    strip_with_fix(shell, glance)
    assert target_still_focused(shell, app)
    assert {"escape", "strip_use"} <= grabs(shell)  # Esc and Alt+Enter, while it is up
    close_strip(shell)
    assert not {"escape", "strip_use"} & grabs(shell)
    assert target_still_focused(shell, app)


def test_hover_menu_and_hand_cursor(desk):
    shell, app, glance = desk
    ov = icon(shell)
    x, y, w, h = ov["status"]["rect"]
    # (the pointer is still where the last click left it, on the icon: away first)
    assert shell.call("TestPointer", {"x": 640, "y": 790})["ok"]
    time.sleep(0.6)
    assert shell.call("TestPointer", {"x": x + w // 2, "y": y + h // 2})["ok"]
    ov = wait_for(lambda: (o := shell.overlay()).get("menu") and o, 5)
    assert ov, shell.overlay()
    assert ov["menu"]["items"][:2] == ["Check spelling & grammar", "Check tone"]
    assert ov["menu"]["hint"] == "Super+Alt+G to check"
    assert ov["cursor"] == "hand"  # the icon is clickable
    shot(shell, "shortcut-menu")
    assert target_still_focused(shell, app)
    # an item runs like a click: the check, then the strip
    assert shell.call("TestClick", {"target": "menu:check"})["ok"]
    strip_with_fix(shell, glance)
    assert wait_for(lambda: not shell.overlay().get("menu"), 3), shell.overlay()
    assert target_still_focused(shell, app)
    # the pointer leaves: the hand goes with it
    assert shell.call("TestPointer", {"x": 640, "y": 790})["ok"]
    assert wait_for(lambda: shell.overlay()["cursor"] == "default", 3), shell.overlay()


MORE = " They has one."  # typed after the checks: a new mistake nobody checks


def test_typing_checks_nothing(desk):
    shell, app, glance = desk
    icon(shell)
    asked = glance.log.read_text(errors="replace").count("check_requested")
    assert shell.call("Chord", {"keys": ["End"]})["ok"]
    assert shell.call("TypeText", {"text": MORE})["ok"]
    assert wait_for(lambda: app.buffer() == TEXT + MORE, 5, 0.1)
    time.sleep(3)  # well past every typing-mode timer (local check, settle, analysis)
    ov = shell.overlay()
    # the edit made the last check stale; nothing checks the new text by itself
    assert ov["status"] and ov["status"]["state"] == "idle" and not ov["status"]["badge"], ov
    assert not ov["strip"]
    state = glance.request("test_state")
    assert state["issues"] == [] and not state["strip"]
    assert glance.log.read_text(errors="replace").count("check_requested") == asked


def test_killed_glanced_lets_go_of_every_key(desk):
    shell, app, glance = desk
    assert shell.call("Chord", {"keys": ["super", "alt", "g"]})["ok"]
    assert wait_for(lambda: placed(shell.overlay().get("strip"))
                    and "escape" in grabs(shell), 15), (shell.overlay(), grabs(shell))
    glance.kill()  # no goodbye: only the extension can let go now
    assert wait_for(lambda: not grabs(shell), 5), shell.call("TestGrabs")
    ov = wait_for(lambda: (o := shell.overlay()) and not (o["strip"] or o["status"]) and o, 5)
    assert ov, shell.overlay()
    # Esc reaches the app again: it goes to the field, not to a grab nobody answers
    assert shell.call("Chord", {"keys": ["Escape"]})["ok"]
    assert shell.call("Chord", {"keys": ["End"]})["ok"]
    assert shell.call("TypeText", {"text": "!"})["ok"]
    assert wait_for(lambda: app.buffer() == TEXT + MORE + "!", 5, 0.1), app.buffer()
    assert target_still_focused(shell, app)
