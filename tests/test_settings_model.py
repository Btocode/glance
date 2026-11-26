"""The Settings window's model: reads, validated writes that keep the user's file, chord
conflicts, the lists that never lose a built-in protection, start at login."""
from pathlib import Path

import pytest

from glance import cli
from glance.audience.overrides import Overrides
from glance.config import load_config
from glance.feature_switches import FEATURES
from glance.privacy.gate import FieldMeta, PrivacyGate
from glance.ui import settings_model as sm

USER_FILE = """# Glance, my way
[general]
check = "typing"   # I like it live

[general.hotkeys]
shot = "Super+Alt+x"  # c is taken here
"""


@pytest.fixture
def rig(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(USER_FILE, encoding="utf-8")
    written: list[int] = []
    model = sm.SettingsModel(path, on_written=lambda: written.append(1), mac=False,
                             overrides=Overrides(tmp_path / "overrides.json"))
    return model, path, written


def field(key: str) -> sm.Field:
    every = [*sm.GENERAL, *(f for fs in sm.FEATURE_FIELDS.values() for f in fs),
             sm.COST_LIMIT, sm.API_PROVIDER, sm.API_MODEL]
    return next(f for f in every if f.id == key)


# ---------------------------------------------------------------------- read / write


def test_reads_the_file_and_the_defaults(rig):
    model, _path, _w = rig
    assert model.value(("general", "check")) == "typing"
    assert model.is_set(("general", "check"))
    assert model.value(("ai", "max_words")) == 600 and not model.is_set(("ai", "max_words"))
    assert model.chords()["shot"] == "Super+Alt+x"


def test_a_write_keeps_comments_and_layout_and_applies_live(rig):
    model, path, written = rig
    assert model.set(field("ai.max_words"), " 900 ") is None
    assert model.set(field("general.check"), "shortcut") is None
    text = path.read_text(encoding="utf-8")
    assert "# Glance, my way" in text and "# I like it live" in text and "# c is taken" in text
    assert 'check = "shortcut"' in text and "max_words = 900" in text
    assert load_config(path).ai.max_words == 900  # what glanced will load
    assert written == [1, 1]  # the live-apply hook, once per write
    assert model.value(("ai", "max_words")) == 900  # the model re-read it


@pytest.mark.parametrize(("key", "raw", "error"), [
    ("ai.max_words", "12", "Enter a number from 50 to 5000"),
    ("ai.max_words", "lots", "Enter a whole number"),
    ("ai.monthly_cost_limit_usd", "-3", "Enter zero or more"),
    ("ai.monthly_cost_limit_usd", "five", "Enter an amount such as 5 or 12.50, or leave it empty"),
    ("selection.translate_to", "  ", "This cannot be empty"),
    ("selection.translate_to", "x" * 41, "At most 40 characters"),
    ("selection.min_words", "51", "Enter a number from 1 to 50"),
    ("backend.api_model", "--danger", "Letters, digits and . _ : / @ + - only"),
    ("shot.ocr", "paper", "Pick one of the choices"),
])
def test_invalid_values_are_refused_and_the_file_is_untouched(rig, key, raw, error):
    model, path, written = rig
    assert model.set(field(key), raw) == error
    assert path.read_text(encoding="utf-8") == USER_FILE
    assert written == []


def test_load_configs_own_checks_guard_every_write(rig):
    model, path, written = rig
    assert model.write(("backend", "per_minute"), -1) == "Expected zero or more"
    assert model.write(("general", "check_updates"), "yes") == "Expected true or false"
    assert path.read_text(encoding="utf-8") == USER_FILE and written == []


def test_a_broken_file_is_never_overwritten(rig):
    model, path, written = rig
    path.write_text("[general\ncheck = ", encoding="utf-8")
    model.refresh()
    assert model.load_error and "cannot be read" in model.load_error
    error = model.set(field("ai.max_words"), "800")
    assert error is not None and error.startswith("config.toml: invalid TOML")
    assert path.read_text(encoding="utf-8") == "[general\ncheck = " and written == []


def test_an_unchanged_value_does_not_touch_the_file(rig):
    model, path, written = rig
    mtime = path.stat().st_mtime_ns
    assert model.set(field("general.check"), "typing") is None
    assert model.set(field("ai.monthly_cost_limit_usd"), "") is None  # unset stays unset
    assert path.stat().st_mtime_ns == mtime and written == []


def test_money_sets_and_clears_the_limit(rig):
    model, path, _w = rig
    assert model.set(field("ai.monthly_cost_limit_usd"), "$12.50") is None
    assert load_config(path).ai.monthly_cost_limit_usd == 12.5
    assert model.set(field("ai.monthly_cost_limit_usd"), "") is None
    assert "monthly_cost_limit_usd" not in path.read_text(encoding="utf-8")


def test_a_model_name_is_written_and_reset_per_backend(rig):
    model, path, _w = rig
    f = sm.model_field("claude")
    assert model.set(f, "sonnet") is None
    assert load_config(path).backend.models["claude"] == "sonnet"
    assert model.set(f, "") is None
    assert load_config(path).backend.models["claude"] == "haiku"  # the default again


def test_features_follow_the_registry_and_write_their_key(rig):
    model, path, written = rig
    rows = model.features()
    assert [f.name for f, _on, _fs in rows] == [f.name for f in FEATURES if f.menu]
    assert "typing" not in [f.name for f, _on, _fs in rows]  # General's "Check my writing"
    assert model.set_feature("shot", False) is None
    assert load_config(path).shot.enabled is False and written == [1]
    names = {f.name: fs for f, _on, fs in model.features()}
    assert [f.id for f in names["shot"]] == ["shot.save_dir", "shot.ocr", "shot.ai_images"]
    assert [f.id for f in names["selection"]] == [
        "selection.translate_to", "selection.min_words", "selection.terminals"]


# ---------------------------------------------------------------------- shortcuts


def test_chord_conflicts():
    chords = {"use": "Alt+Return", "check": "Super+Alt+g", "panel": "Super+Alt+space"}
    assert sm.chord_problem("check", "Super+Alt+g", chords) is None
    assert sm.chord_problem("check", "super+ALT+space", chords) == \
        "Glance already uses it for “Open the panel and tones”"
    assert sm.chord_problem("check", "Super+Alt+s", chords) == \
        "Used by GNOME: screen reader (Orca) on/off"
    assert sm.chord_problem("check", "g", chords).startswith("Add Ctrl, Alt or Super")
    assert sm.chord_problem("check", "Hyper+g", chords) == "Not a valid shortcut"
    # the Mac's own table is used there, not the Linux one
    assert sm.chord_problem("check", "Super+Alt+space", {}, mac=True) == \
        "Used by Finder search window"
    assert sm.chord_problem("check", "Super+Alt+s", {}, mac=True) is None


def test_the_live_grab_result_counts_only_for_the_chord_it_was_about():
    live = {"check": "conflict"}
    assert sm.chord_problem("check", "Super+Alt+g", {}, live=live,
                            live_chord="Super+Alt+g") == "Another program already uses it"
    assert sm.chord_problem("check", "Super+Alt+j", {}, live=live,
                            live_chord="Super+Alt+g") is None
    assert sm.parse_live_problems("check:system,shot:conflict") == {
        "check": "system", "shot": "conflict"}
    assert sm.parse_live_problems(None) == {}


def test_chord_from_keys():
    assert sm.chord_from_keys(["alt", "super"], "G") == "Super+Alt+g"
    assert sm.chord_from_keys(["ctrl", "shift"], "Return") == "Ctrl+Shift+Return"
    assert sm.chord_from_keys(["super"], "Super_L") is None  # a modifier on its own


def test_shortcut_rows_set_and_reset(rig):
    model, path, written = rig
    rows = {r.name: r for r in model.shortcuts({"pause": "conflict"})}
    assert list(rows)[:3] == ["use", "check", "panel"]
    assert rows["shot"].chord == "Super+Alt+x" and not rows["shot"].is_default
    assert rows["pause"].problem == "Another program already uses it"
    assert rows["use"].label == "Apply the suggestion" and rows["tone1"].label == "Tone 1"
    assert model.set_shortcut("check", "Super+Alt+k") == \
        "Used by KDE Plasma: next keyboard layout"
    assert model.set_shortcut("check", "Super+Alt+x") == \
        "Glance already uses it for “Take a Glance Shot (capture a region)”"
    assert written == []
    assert model.set_shortcut("check", "Super+Alt+j") is None
    assert load_config(path).general.hotkeys["check"] == "Super+Alt+j"
    assert model.set_shortcut("use", "Ctrl+Return") is None
    assert load_config(path).ui.use_key == "Ctrl+Return"
    assert model.reset_shortcut("shot") is None
    text = path.read_text(encoding="utf-8")
    assert "Super+Alt+x" not in text and "# Glance, my way" in text
    assert model.set_shortcut("check", "Super+Alt+g") is None  # the default: line dropped
    import tomli

    assert "check" not in tomli.loads(path.read_text(encoding="utf-8"))["general"]["hotkeys"]


def test_mac_shortcuts_show_the_mac_defaults(tmp_path):
    model = sm.SettingsModel(tmp_path / "config.toml", mac=True)
    rows = {r.name: r for r in model.shortcuts()}
    assert rows["check"].is_default
    from glance.platform.macos.hotkeys_mac import MAC_DEFAULTS

    assert rows["check"].chord == MAC_DEFAULTS["check"]


# ---------------------------------------------------------------------- privacy lists


def test_adding_to_a_list_keeps_every_built_in_protection(rig):
    model, path, _w = rig
    builtin = [i.value for i in model.list_items("app_denylist")]
    assert all(i.builtin for i in model.list_items("app_denylist"))
    assert model.add_to_list("app_denylist", "signal") is None
    cfg = load_config(path)
    assert list(cfg.privacy.app_denylist) == [*builtin, "signal"]
    assert model.add_to_list("app_denylist", "Signal") == "Already in the list"
    assert model.remove_from_list("app_denylist", "keepassxc") == \
        "A built-in protection cannot be removed"
    assert model.remove_from_list("app_denylist", "signal") is None
    assert list(load_config(path).privacy.app_denylist) == builtin


def test_url_patterns_must_compile(rig):
    model, path, _w = rig
    assert model.add_to_list("url_denylist", "intranet(") == \
        "Not a valid pattern (a regular expression)"
    assert model.add_to_list("url_denylist", r"intranet\.example\.com") is None
    assert r"intranet\.example\.com" in load_config(path).privacy.url_denylist
    assert "bank" in load_config(path).privacy.url_denylist


def test_terminal_allowlist_add_and_remove(rig):
    model, path, _w = rig
    assert model.add_to_list("terminal_allowlist", "claude") is None
    assert load_config(path).privacy.terminal_allowlist == ("claude",)
    assert model.remove_from_list("terminal_allowlist", "claude") is None
    assert load_config(path).privacy.terminal_allowlist == ()


# ---------------------------------------------------------------------- apps


def _meta(wm: str) -> FieldMeta:
    return FieldMeta(app=wm, wm_class=wm, title="t", url=None, role="text",
                     states=frozenset({"editable"}), pid=1, text_len=5)


def test_per_app_off_reaches_the_gate_even_with_dots_in_the_class(rig):
    model, path, _w = rig
    assert model.add_app("org.gnome.TextEditor") is None
    assert model.set_app_capture("org.gnome.texteditor", False) is None
    cfg = load_config(path)
    assert PrivacyGate(cfg).evaluate(_meta("org.gnome.TextEditor")).reason == "app_disabled"
    assert PrivacyGate(cfg).evaluate(_meta("gedit")).allowed
    assert [a.wm_class for a in model.apps()] == ["org.gnome.texteditor"]
    assert model.add_app("org.gnome.texteditor") == "Already in the list"
    assert model.add_app("slack|general").startswith("Type the app's window class")
    assert model.remove_app("org.gnome.texteditor") is None
    assert PrivacyGate(load_config(path)).evaluate(_meta("org.gnome.TextEditor")).allowed


def test_per_app_audience_profile(rig, tmp_path):
    model, _path, _w = rig
    assert ("email", "Email") in model.profiles()
    assert model.set_app_profile("thunderbird", "email") is None
    assert Overrides(tmp_path / "overrides.json").get("thunderbird") == "email"
    assert [(a.wm_class, a.profile) for a in model.apps()] == [("thunderbird", "email")]
    assert model.set_app_profile("thunderbird", "nonsense") == "Unknown profile"
    assert model.set_app_profile("thunderbird", None) is None
    assert model.apps() == ()


# ---------------------------------------------------------------------- the rest


def test_start_at_login_linux():
    calls = []

    def run(argv):
        calls.append(argv)
        return (0, "enabled\n") if "is-enabled" in argv else (0, "")

    login = sm.StartAtLogin(mac=False, run=run, uid=501)
    assert login.get() is True
    assert login.set(False) is True
    assert calls[-1] == ["systemctl", "--user", "disable", "glance.service"]
    assert sm.StartAtLogin(mac=False, run=lambda a: (1, "disabled\n")).get() is False
    assert sm.StartAtLogin(mac=False, run=lambda a: (4, "")).get() is None


def test_start_at_login_mac():
    out = 'disabled services = {\n\t"io.github.btocode.glance" => disabled\n}\n'
    calls = []

    def run(argv):
        calls.append(argv)
        return 0, out

    login = sm.StartAtLogin(mac=True, run=run, uid=501)
    assert login.get() is False
    assert calls[-1] == ["launchctl", "print-disabled", "gui/501"]
    login.set(True)
    assert calls[-1] == ["launchctl", "enable", "gui/501/io.github.btocode.glance"]
    assert sm.StartAtLogin(mac=True, run=lambda a: (0, "{}"), uid=1).get() is True


def test_settings_request_opens_a_known_section_only():
    opened = []
    handle = sm.settings_request(opened.append)
    assert handle({}) == {"ok": True} and handle({"section": "ai"}) == {"ok": True}
    assert handle({"section": "nope"})["ok"] is False
    assert opened == [None, "ai"]


def test_cli_knows_the_sections():
    assert cli.SETTINGS_SECTIONS == tuple(k for k, _ in sm.SECTIONS)


def test_summaries():
    assert sm.usage_summary(None).today == "Today: no AI calls"
    total = {"calls": 3, "failed": 0, "input_tokens": 10, "output_tokens": 5,
             "tokens_known": True, "cost_usd": 0.02}
    s = sm.usage_summary({"periods": {"today": {"total": total}},
                          "monthly_cost_limit_usd": 5.0, "limit_reason": None})
    assert s.today == "Today: 3 calls · 15 tokens · $0.02"
    assert s.month == "This month: no AI calls" and s.limit == "Limit: $5 a month"
    from types import SimpleNamespace as R

    assert sm.doctor_summary([R(status="ok"), R(status="ok")]) == "All 2 checks passed"
    assert sm.doctor_summary([R(status="fail"), R(status="warn"), R(status="ok")]) == \
        "1 failed, 1 to look at of 3 checks"
    assert sm.backend_status({"installed": False}) == "Not installed"
    assert sm.backend_status({"installed": True, "signed_in": True}) == "Signed in"
    assert sm.backend_status(None) == "Not checked yet"
