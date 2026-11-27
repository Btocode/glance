"""Glance Shot on GNOME Wayland, in a private headless GNOME Shell (never the user's session).

    GLANCE_WAYLAND_TEST=1 tests/wayland/private-session.sh ~/glance-wltest-shot \\
        .venv/bin/python -m pytest -m wayland tests/wayland/test_wayland_shot.py

The shortcut opens the extension's picker; a drag with the shell's virtual pointer picks the
test app's window; the image comes back to glanced through `ShotCapture` (refused to anyone
else); the bar's Copy puts image/png on the clipboard; Esc closes it. With
$GLANCE_TEST_OCR_MODELS (a directory holding the two ocrs models) and glance_core 0.3,
Extract text reads the app's text on the device.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from tests.wayland.harness import Glance, GtkApp, Shell, gsettings, private_root, wait_for

pytestmark = [
    pytest.mark.wayland,
    pytest.mark.skipif(os.environ.get("GLANCE_WAYLAND_TEST") != "1",
                       reason="set GLANCE_WAYLAND_TEST=1 (and run in private-session.sh)"),
    pytest.mark.skipif(private_root() is None,
                       reason="only inside tests/wayland/private-session.sh"),
    pytest.mark.skipif(shutil.which("gnome-shell") is None, reason="no gnome-shell"),
]

TEXT = "Deploy failed: connection refused on port 8443."
SHOTS = os.environ.get("GLANCE_WAYLAND_SHOTS")
CONFIG = '[shot]\nocr = "ocrs"\nai_images = "ask"\n'


@pytest.fixture(scope="module")
def desk():
    root = private_root()
    assert root is not None
    models = os.environ.get("GLANCE_TEST_OCR_MODELS")
    if models:  # the pinned models, as a first Extract text would have fetched them
        dest = Path(os.environ["XDG_DATA_HOME"]) / "glance" / "models" / "ocrs"
        dest.mkdir(parents=True, exist_ok=True)
        for name in ("text-detection.onnx", "text-recognition.onnx"):
            shutil.copyfile(Path(models) / name, dest / name)
    shell = Shell(root).start()
    app = glance = None
    try:
        app = GtkApp(shell, TEXT)
        glance = Glance(shell, root, CONFIG, name="glanced-shot")
        assert wait_for(lambda: glance.status().get("shell_extension") == "running", 20)
        # the extension holds the chord (not just glanced believing so)
        assert wait_for(lambda: any(g["name"] == "shot"
                                    for g in shell.call("TestGrabs").get("grabs", [])), 20)
        yield shell, app, glance, root
    finally:
        for part in (glance, app):
            if part is not None:
                part.stop()
        shell.stop()


def snap(shell: Shell, name: str) -> None:
    if not SHOTS:
        return
    Path(SHOTS).mkdir(parents=True, exist_ok=True)
    for theme, scheme, gtk in (("light", "prefer-light", "Adwaita"),
                               ("dark", "prefer-dark", "Adwaita-dark")):
        gsettings("set", "org.gnome.desktop.interface", "color-scheme", scheme)
        gsettings("set", "org.gnome.desktop.interface", "gtk-theme", gtk)
        time.sleep(1.0)
        shell.screenshot(Path(SHOTS) / f"wayland-{name}-{theme}.png")
    gsettings("set", "org.gnome.desktop.interface", "color-scheme", "default")
    gsettings("reset", "org.gnome.desktop.interface", "gtk-theme")
    time.sleep(0.5)


def pointer(shell: Shell, x: int, y: int) -> None:
    shell.call("TestPointer", {"x": x, "y": y})
    shell.call("TestPointer", {"x": x, "y": y})
    time.sleep(0.05)


def button(shell: Shell, down: bool) -> None:
    shell.call("TestPointer", {"x": 1 if down else 0, "how": "button"})
    time.sleep(0.08)


def shot_state(shell: Shell) -> dict:
    return shell.overlay().get("shot") or {}


def test_shortcut_opens_the_picker_and_a_drag_captures(desk):
    shell, _app, glance, _root = desk
    assert wait_for(lambda: shell.overlay().get("status"), 30)  # glanced is all there
    frame = shell.focused()["frame"]
    # the user presses Super+Alt+C (the shell's own virtual keyboard); a keybinding added a
    # moment ago may need a second press in the headless shell
    for _ in range(3):
        assert shell.call("Chord", {"keys": ["super", "alt", "c"]})["ok"]
        if wait_for(lambda: shot_state(shell).get("picking"), 4):
            break
    assert shot_state(shell).get("picking"), shell.overlay()
    x0, y0 = frame[0] + 4, frame[1] + 4
    x1, y1 = frame[0] + frame[2] - 4, frame[1] + min(frame[3], 160)
    pointer(shell, x0, y0)
    button(shell, True)
    for i in range(1, 6):
        pointer(shell, x0 + (x1 - x0) * i // 5, y0 + (y1 - y0) * i // 5)
    assert wait_for(lambda: shot_state(shell).get("label"), 5), shot_state(shell)
    snap(shell, "shot-picker")
    button(shell, False)
    st = wait_for(lambda: (s := glance.status()).get("shot_open") and s, 15)
    assert st, glance.status()
    w, h = (int(v) for v in st["shot_size"].split("x"))
    assert w >= x1 - x0 - 2 and h >= y1 - y0 - 2  # device pixels: the scale may be 1 or 2
    bar = wait_for(lambda: shot_state(shell).get("bar"), 5)
    assert bar, shot_state(shell)
    # one card beside the region, never over it; the region outlined just outside it
    bx, by, bw, bh = bar
    assert bx >= x1 or bx + bw <= x0 or by >= y1 or by + bh <= y0, (bar, (x0, y0, x1, y1))
    fx, fy, fw, fh = shot_state(shell)["frame"]
    assert fx < x0 and fy < y0 and fx + fw > x1 and fy + fh > y1
    named = shot_state(shell)["named"]
    for key in ("shot:copy", "shot:save", "shot:extract", "shot:explain", "shot:translate",
                "shot:ask", "shot:body", "shot:close"):
        assert key in named, named
    assert not shot_state(shell)["picking"]
    snap(shell, "shot-start")


def test_capture_is_refused_to_anyone_but_glanced(desk):
    from glance.shellbus import ShellUnavailable

    shell, _app, _glance, _root = desk
    with pytest.raises(ShellUnavailable):
        shell.call_directly("ShotCapture", {"x": 0, "y": 0, "w": 10, "h": 10})
    with pytest.raises(ShellUnavailable):
        shell.call_directly("ShotPick", {})


def test_copy_puts_the_image_on_the_clipboard(desk):
    shell, _app, _glance, _root = desk
    assert shell.call("TestClick", {"target": "shot:copy"})["ok"]
    bar = shot_state(shell)["bar"]
    assert wait_for(lambda: shot_state(shell).get("banner") == "Image copied to the clipboard", 5)
    assert shot_state(shell)["bar"][:2] == bar[:2]  # the same card, in the same place
    # the clipboard holds image/png now: not plain text a save could restore
    saved = shell.call("ClipboardSave", {"which": "clipboard"})
    assert saved == {"restorable": False}, saved


def test_ask_without_a_vision_ai_says_so(desk):
    shell, _app, glance, _root = desk
    assert shell.call("TestClick", {"target": "shot:ask"})["ok"]
    assert wait_for(lambda: "vision-capable" in (shot_state(shell).get("banner") or ""), 5), \
        shot_state(shell)
    assert glance.status()["shot_result"] is None  # nothing was sent anywhere
    assert shell.call("TestClick", {"target": "shot:explain"})["ok"]
    assert wait_for(lambda: "Needs an AI" in (shot_state(shell).get("banner") or ""), 5), \
        shot_state(shell)
    assert glance.status()["shot_result"] is None


def test_escape_closes_the_bar(desk):
    shell, app, glance, _root = desk
    assert shell.call("Chord", {"keys": ["Escape"]})["ok"]
    assert wait_for(lambda: not glance.status()["shot_open"], 5)
    assert wait_for(lambda: not shot_state(shell).get("bar"), 5)
    focus = shell.focused() or {}
    assert focus.get("pid") == app.proc.pid  # the app has its window back
def test_text_is_read_into_the_card(desk):
    """Text reads the window on the device and shows it in the shot's own card (the same
    card, in the same place), with Copy text and Fix grammar under it."""
    import importlib

    core = importlib.util.find_spec("glance_core")
    if not os.environ.get("GLANCE_TEST_OCR_MODELS") or core is None:
        pytest.skip("needs GLANCE_TEST_OCR_MODELS and glance_core 0.3")
    import glance_core

    if not hasattr(glance_core, "Ocr"):
        pytest.skip("glance_core < 0.3")
    shell, app, glance, _root = desk
    if not glance.status().get("shot_open"):  # Escape closed it: take another shot
        frame = shell.focused()["frame"]
        for _ in range(3):
            assert shell.call("Chord", {"keys": ["super", "alt", "c"]})["ok"]
            if wait_for(lambda: shot_state(shell).get("picking"), 4):
                break
        x0, y0 = frame[0] + 4, frame[1] + 4
        x1, y1 = frame[0] + frame[2] - 4, frame[1] + min(frame[3], 160)
        pointer(shell, x0, y0)
        button(shell, True)
        for i in range(1, 6):
            pointer(shell, x0 + (x1 - x0) * i // 5, y0 + (y1 - y0) * i // 5)
        button(shell, False)
        assert wait_for(lambda: "shot:extract" in shot_state(shell).get("named", []), 15)
    bar = shot_state(shell)["bar"]
    assert shell.call("TestClick", {"target": "shot:extract"})["ok"]
    st = wait_for(lambda: "shot:text_fix" in shot_state(shell).get("named", [])
                  and shot_state(shell), 30)
    assert st, (shot_state(shell), glance.status())
    assert glance.status()["shot_result"] == "ready"
    assert st["selected"] == ["extract"] and "shot:text_copy" in st["named"]
    assert st["bar"][:2] == bar[:2]  # the same card, in the same place
    assert (shell.focused() or {}).get("pid") == app.proc.pid  # focus stays with the app
    snap(shell, "shot-text")
