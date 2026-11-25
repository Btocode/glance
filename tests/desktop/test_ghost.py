"""The ghost overlay and the transient Tab / Ctrl+Right grabs on a private Xvfb server.

    .venv/bin/pytest -m desktop tests/desktop/test_ghost.py -v

Never touches the user's display: `ghost_probe.py` runs against its own Xvfb and sends XTEST
keys to a TextView in a window of its own.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_xvfb import xvfb  # noqa: F401 - the fixture

pytestmark = [pytest.mark.desktop,
              pytest.mark.skipif(shutil.which("Xvfb") is None, reason="Xvfb is not installed")]

PROBE = Path(__file__).with_name("ghost_probe.py")
TEXT = "I will send it"
FULL = TEXT + " tomorrow morning."


@pytest.fixture(scope="module")
def probe(xvfb):  # noqa: F811
    env = {**os.environ, "DISPLAY": xvfb, "NO_AT_BRIDGE": "1", "GDK_BACKEND": "x11"}
    env.pop("WAYLAND_DISPLAY", None)
    done = subprocess.run([sys.executable, str(PROBE)], env=env, capture_output=True,
                          text=True, timeout=120, check=False)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_tab_untouched_when_no_suggestion(probe):
    assert probe["start_grabs"] == ["complete"]
    assert probe["tabs_inserted"] == 200 and probe["tabs_text_is_only_tabs"]
    assert probe["idle_grabs"] == []


def test_tab_accepts_suggestion(probe):
    assert probe["grabs_while_visible"] == ["ac_tab", "ac_word"]
    assert probe["ghost_visible_while_visible"] is True
    assert probe["after_tab"] == FULL and "\t" not in probe["after_tab"]
    assert probe["visible_after_tab"] is False and probe["grabs_after_tab"] == []
    assert probe["ghost_visible_after_tab"] is False


def test_tab_normal_again_after_accept(probe):
    assert probe["tab_after_accept"] == "\t"


def test_ctrl_right_accepts_one_word(probe):
    assert probe["after_ctrl_right"] == TEXT + " tomorrow"
    assert probe["visible_after_ctrl_right"] is True
    assert probe["ghost_text_after_ctrl_right"] == " morning."
    assert probe["grabs_after_ctrl_right"] == ["ac_tab", "ac_word"]
    assert probe["after_second_ctrl_right"] == FULL
    assert probe["visible_after_second_ctrl_right"] is False


def test_typing_dismisses(probe):
    assert probe["dismissed_in_s"] is not None and probe["dismissed_in_s"] < 0.2
    assert probe["grabs_after_typing"] == []
    assert probe["ghost_visible_after_typing"] is False
    assert probe["text_after_typing_then_tab"] == TEXT + "x\t"


def test_ghost_is_click_through_and_unfocused(probe):
    assert probe["click_inside_ghost"] is True
    assert probe["caret_before_click"] == 0
    assert probe["caret_after_click"] in (probe["caret_wanted"], probe["caret_wanted"] + 1)
    assert probe["focus_is_app"] is True
    assert probe["ghost_override_redirect"] is True
    assert probe["ghost_input_rects"] == 0
    assert probe["ghost_flags"] == [False, False, False]


def test_ghost_font_is_the_given_pixel_size(probe):
    assert probe["ghost_font_px"] == 16


def test_ghost_is_clamped_to_the_monitor_and_ellipsised(probe):
    x, y, w, h = probe["clamped"]
    assert x >= 0 and y >= 0 and x + w <= 1280 and y + h <= 800
    x, y, w, h = probe["narrow"]
    assert (x, y) == (100, 100) and w <= 150
