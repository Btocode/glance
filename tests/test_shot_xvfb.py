"""Glance Shot's X11 renderer on a private Xvfb: the picker freezes and dims the screen, a real
(XTEST) drag picks a region that is captured into memory, the bar sits under it, nothing takes
the input focus, and Copy puts the image on the clipboard. Never the user's display."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROBE = Path(__file__).parent / "desktop" / "shot_probe.py"

pytestmark = pytest.mark.skipif(shutil.which("Xvfb") is None, reason="Xvfb is not installed")


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    read_fd, write_fd = os.pipe()
    xvfb = subprocess.Popen(
        ["Xvfb", "-displayfd", str(write_fd), "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
        pass_fds=[write_fd], stderr=subprocess.DEVNULL)
    os.close(write_fd)
    try:
        with os.fdopen(read_fd) as f:
            number = f.readline().strip()
        assert number, "Xvfb did not start"
        shots = Path(os.environ.get("GLANCE_UI_SHOTS") or tmp_path_factory.mktemp("shots"))
        shots.mkdir(parents=True, exist_ok=True)
        home = tmp_path_factory.mktemp("home")
        env = {**os.environ, "DISPLAY": f":{number}", "NO_AT_BRIDGE": "1", "GDK_BACKEND": "x11",
               "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config")}
        env.pop("WAYLAND_DISPLAY", None)
        done = subprocess.run([sys.executable, str(PROBE), "--shots", str(shots)], env=env,
                              capture_output=True, text=True, timeout=90, check=False)
        assert done.returncode == 0, done.stderr[-2000:]
        out = json.loads(done.stdout.strip().splitlines()[-1])
        out["shots_dir"] = str(shots)
        yield out
    finally:
        xvfb.terminate()
        xvfb.wait(timeout=5)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_picker_never_takes_the_focus_and_captures_the_drag(probe, theme):
    p = probe[f"picker_{theme}"]
    assert p["override_redirect"] and p["focusable"][:2] == [False, False]
    assert p["grabbed"] == ["shot_pick_escape", "shot_pick_return", "shot_pick_space"]
    assert p["size"] == [1280, 800]
    assert probe[f"focus_while_picking_{theme}"] is True
    assert probe[f"label_{theme}"] == "780 × 324"
    assert probe[f"result_{theme}"]["rect"] == [150, 108, 780, 324]
    assert probe[f"result_{theme}"]["png"] == [780, 324]
    assert probe[f"picker_closed_{theme}"]  # window gone, keys let go


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_bar_under_the_region_without_focus(probe, theme):
    x, y, w, _h = probe[f"bar_{theme}"]
    assert y >= 120 + 300 + 8 - 10  # under the region (less the shadow margin)
    assert x + w <= 160 + 760 + 20
    assert probe[f"bar_override_redirect_{theme}"] and probe[f"bar_focus_{theme}"] == [False,
                                                                                       False]
    assert probe[f"clicked_{theme}"] == [True, True]
    assert probe[f"acts_{theme}"] == ["notice_send", "copy"]
    assert probe[f"focus_after_bar_{theme}"] is True


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_card_steps_ask_in_place_and_mark(probe, theme):
    assert probe[f"card_stays_put_{theme}"]  # the toolbar never moves while the card grows
    assert probe[f"ask_typed_{theme}"] == [["ask_submit", {"text": "hi"}]]
    assert probe[f"focus_after_ask_{theme}"] is True  # the app kept its focus
    assert probe[f"mark_strokes_{theme}"] == ["box", "arrow"]
    assert probe[f"marked_{theme}"] is True


def test_keys_and_clipboard(probe):
    assert probe["escape_cancels"] is True
    assert probe["enter_full"] == [1280, 800]
    assert probe["clipboard"] == [True, [1280, 800]]


def test_screenshots(probe):
    shots = Path(probe["shots_dir"])
    for name in ("picker", "bar", "notice"):
        for theme in ("light", "dark"):
            assert (shots / f"shot-{name}-{theme}.png").stat().st_size > 2000, name
