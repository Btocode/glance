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
    assert bar and bar[1] > y0, shot_state(shell)
    assert "shot:copy" in shot_state(shell)["named"]
    assert not shot_state(shell)["picking"]
    snap(shell, "shot-bar")


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
    assert wait_for(lambda: shot_state(shell).get("banner") == "Copied to the clipboard", 5)
    # the clipboard holds image/png now: not plain text a save could restore
    saved = shell.call("ClipboardSave", {"which": "clipboard"})
    assert saved == {"restorable": False}, saved


def test_explain_without_a_vision_ai_says_so(desk):
    shell, _app, glance, _root = desk
    assert shell.call("TestClick", {"target": "shot:explain"})["ok"]
    assert wait_for(lambda: "vision-capable" in (shot_state(shell).get("banner") or ""), 5), \
        shot_state(shell)
    assert glance.status()["shot_result"] is None  # nothing was sent anywhere


def test_escape_closes_the_bar(desk):
    shell, app, glance, _root = desk
    assert shell.call("Chord", {"keys": ["Escape"]})["ok"]
    assert wait_for(lambda: not glance.status()["shot_open"], 5)
    assert wait_for(lambda: not shot_state(shell).get("bar"), 5)
    focus = shell.focused() or {}
    assert focus.get("pid") == app.proc.pid  # the app has its window back
def test_extract_text_opens_in_the_selection_result_panel(desk):
    """Extract text reads the window on the device and hands the text to the selection
    actions' result panel (Copy, Fix grammar, Translate, Explain, Ask a follow-up)."""
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
    assert shell.call("TestClick", {"target": "shot:extract"})["ok"]
    ov = wait_for(lambda: (o := shell.overlay()).get("panel")
                  and o["panel"]["mode"] == "result"
                  and "res_action:fix" in o["named"] and o, 30)
    assert ov, (shell.overlay().get("panel"), glance.status())
    result = ov["panel"]["result"]
    assert result["state"] == "ready" and "8443" in (result["text"] or ""), result
    assert result["actions"][0] == "fix" and "res_copy" in ov["named"]
    assert not glance.status()["shot_open"]  # the shot bar made way for the panel
    assert (shell.focused() or {}).get("pid") == app.proc.pid  # focus stays with the app
    snap(shell, "shot-text")
