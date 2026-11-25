from glance.backends.base import CONNECT, SIGN_IN, Health
from glance.doctor import checks
from glance.ui.onboarding_model import STORAGE_NOTES, build_rows, headline

NONE_READY = {"claude": Health(False, None, ""), "codex": Health(True, False, ""),
              "gemini": Health(False, None, ""), "opencode": Health(False, None, ""),
              "api": Health(True, False, "")}


def test_rows_states_and_commands():
    rows = build_rows({"claude": Health(True, True, ""), "codex": Health(True, False, ""),
                       "gemini": Health(True, None, ""), "opencode": Health(False, None, ""),
                       "api": Health(True, False, "")}, None)
    assert [(r.name, r.state, r.selectable) for r in rows] == [
        ("claude", "ready", True), ("codex", "signed_out", False),
        ("gemini", "unknown", True), ("opencode", "not_installed", False),
        ("api", "signed_out", False)]
    assert [r.command for r in rows] == [
        "claude", "codex login", "gemini", "opencode auth login", "glance backend set-key"]
    assert all(r.storage_note.endswith("under its own terms.") for r in rows)
    assert set(STORAGE_NOTES) == {"claude", "codex", "gemini", "opencode", "api"}


def test_missing_health_is_unknown_and_order_is_fixed():
    rows = build_rows({}, None)
    assert [r.name for r in rows] == ["claude", "codex", "gemini", "opencode", "api"]
    assert {r.state for r in rows} == {"unknown"}


def test_selected_flag_follows_choice():
    rows = build_rows(NONE_READY, "codex")
    assert [r.name for r in rows if r.selected] == ["codex"]


def test_headline():
    assert headline(build_rows(NONE_READY, None)) == "Connect an AI"
    assert headline(build_rows(NONE_READY, "claude")) == "Connect an AI"
    ready = {**NONE_READY, "claude": Health(True, True, "")}
    assert headline(build_rows(ready, None)) == "Choose your AI"
    assert headline(build_rows(ready, "claude")) == "AI: Claude"
    # the chosen one signed out while another is ready: choose again
    assert headline(build_rows(ready, "codex")) == "Choose your AI"


def test_one_sign_in_table_for_doctor_and_onboarding():
    """Spec 5.2: the exact command per backend, once."""
    assert CONNECT == {"claude": "claude", "codex": "codex login", "gemini": "gemini",
                       "opencode": "opencode auth login", "api": "glance backend set-key"}
    assert SIGN_IN == {
        "claude": "run `claude` and log in",
        "codex": "run `codex login`",
        "gemini": "run `gemini` to authenticate (or use an API key)",
        "opencode": "run `opencode auth login`",
        "api": "run `glance backend set-key`",
    }
    assert checks.SIGN_IN is SIGN_IN
