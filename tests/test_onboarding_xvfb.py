"""The onboarding window and the tray's backend menu, on a private Xvfb (never the user's)."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_xvfb import xvfb  # noqa: F401  (the private Xvfb fixture)

PROBE = Path(__file__).parent / "desktop" / "onboarding_probe.py"

pytestmark = pytest.mark.skipif(shutil.which("Xvfb") is None, reason="Xvfb is not installed")


@pytest.fixture(scope="module")
def probe(xvfb):  # noqa: F811
    env = {**os.environ, "DISPLAY": xvfb, "NO_AT_BRIDGE": "1", "GDK_BACKEND": "x11"}
    env.pop("WAYLAND_DISPLAY", None)
    done = subprocess.run([sys.executable, str(PROBE)], env=env, capture_output=True,
                          text=True, timeout=90, check=False)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_window_shows_state_command_and_notes_without_blocking(probe):
    # ticks_during_refresh proves the 0.3 s health check runs off the main loop; this bound
    # only catches a window that waits for it (CI runners are slow to draw)
    assert probe["show_seconds"] < 1.0
    assert probe["ticks_during_refresh"] >= 4
    assert probe["threads_off_main"] is True
    texts = probe["texts"]
    assert "Choose your AI" in texts  # claude is ready and nothing is chosen yet
    for cmd in ("claude", "codex login", "gemini", "opencode auth login",
                "glance backend set-key"):
        assert cmd in texts
    assert probe["test_note_count"] == 5
    assert any(t.endswith("under its own terms.") for t in texts)
    # claude ready, codex signed out, gemini unknown, opencode not installed, api signed out
    assert probe["use_sensitive"] == [True, False, True, False, False]


def test_test_button_reports_pass_and_fail(probe):
    assert probe["testing_text"] is True
    assert probe["test_result"] == ["✓ ok"]
    assert probe["test_results_after_gemini"][-1].startswith("✗ auth")


def test_use_this_chooses_and_marks_in_use(probe):
    assert probe["used"] == ["claude"]
    assert probe["headline_after_use"] == ["AI: Claude"]
    assert probe["in_use_button"] == ["In use"]


def test_api_row_key_is_hidden_cleared_and_handed_over(probe):
    assert probe["key_visible"] is False
    assert probe["key_after_click_in_widget"] == ""
    assert probe["key_calls"] == [["openai", "sk-probe-key"]]
    assert probe["api_calls"] == [["openai", "gpt-x", ""]]
    assert probe["key_status"] == "Saved in the system keyring"


def test_closing_hides_the_window_so_the_tray_can_reopen_it(probe):
    assert probe["hidden_not_destroyed"] is True


def test_tray_backend_menu(probe):
    assert probe["menu_none"] == [
        "No AI connected. Sign in to one:",
        "Claude: run `claude` and log in", "Codex: run `codex login`",
        "Gemini: run `gemini` to authenticate (or use an API key)",
        "OpenCode: run `opencode auth login`", "API key: run `glance backend set-key`",
        "Connect an AI…"]
    assert probe["menu_ready"] == ["Claude", "Set up…"]
    assert probe["menu_active"] == ["Claude"]
    assert probe["menu_picked_on_rebuild"] == []
    assert probe["menu_picked"] == ["codex"]
    assert probe["menu_setup"] == [1]
