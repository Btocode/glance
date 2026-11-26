"""The Settings window on a private Xvfb and a private D-Bus session (never the user's):
every section draws in light and dark, writes keep the file, a taken shortcut is refused,
glanced is only ever asked from a worker thread, the API key never stays in a widget."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_xvfb import xvfb  # noqa: F401  (the private Xvfb fixture)

PROBE = Path(__file__).parent / "desktop" / "settings_probe.py"

pytestmark = pytest.mark.skipif(
    shutil.which("Xvfb") is None or shutil.which("dbus-run-session") is None,
    reason="Xvfb or dbus-run-session is not installed")


@pytest.fixture(scope="module")
def probe(xvfb, tmp_path_factory):  # noqa: F811
    home = tmp_path_factory.mktemp("home")
    env = {**os.environ, "DISPLAY": xvfb, "NO_AT_BRIDGE": "1", "GDK_BACKEND": "x11",
           "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
           "XDG_DATA_HOME": str(home / ".local" / "share")}
    for name in ("WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "GLANCE_SHOTS_DIR"):
        env.pop(name, None)
    if os.environ.get("GLANCE_SHOTS_DIR"):
        env["GLANCE_SHOTS_DIR"] = os.environ["GLANCE_SHOTS_DIR"]
    done = subprocess.run(["dbus-run-session", "--", sys.executable, str(PROBE)], env=env,
                          capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_opens_fast_and_asks_glanced_off_the_main_loop(probe):
    assert probe["open_seconds"] < 1.0  # CI runners are slow to draw; this is ~0.15 s
    assert probe["calls_off_main"] is True
    assert len(set(probe["widths"])) == 1  # no section widens the window


def test_every_section_draws(probe):
    assert "Check my writing" in probe["texts_general"]
    assert "Start Glance at login" in probe["texts_general"]
    for label in ("Selection actions", "Glance Shot", "Autocomplete while typing",
                  "Save shots to", "Send images to the AI", "Translate to"):
        assert label in probe["texts_features"]
    for label in ("Claude", "Signed in", "In use", "Not installed", "Key saved",
                  "Monthly cost limit (US$)", "Details…"):
        assert label in probe["texts_ai"]
    assert any(t.startswith("Today: 26 calls") for t in probe["texts_ai"])
    assert "Take a Glance Shot (capture a region)" in probe["texts_shortcuts"]
    assert "keepassxc" in probe["texts_apps"] and "slack" in probe["texts_apps"]
    assert "Pages Glance never reads" in probe["texts_privacy"]
    assert "1 to look at of 3 checks" in probe["texts_about"]


def test_writes_are_validated_and_keep_the_file(probe):
    assert probe["write_ok"] is None
    assert probe["write_bad"] == "Enter a number from 50 to 5000"
    assert probe["file_keeps_comment"] is True and probe["written"] == 1


def test_a_taken_shortcut_is_refused_and_a_free_one_written(probe):
    assert probe["conflict_text"] == ["Super+Alt+k: Used by KDE Plasma: next keyboard layout"]
    assert probe["chord_written"] is True
    assert probe["live_problem"] is True


def test_the_api_key_is_handed_over_and_not_kept(probe):
    assert probe["key_calls"] == [["set_key", "anthropic", 15]]
    assert probe["key_left_in_widget"] is False
    assert probe["opened"] == []
