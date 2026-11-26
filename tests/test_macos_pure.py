"""The macOS logic that needs no Mac: coordinates, UTF-16 offsets, AX roles and the privacy
lists, key planning, hotkey chords and the system shortcuts, the LaunchAgent, power, the
notification script. All of it runs on Linux; nothing here imports PyObjC."""
from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from glance.config import Config
from glance.platform.macos import ax_roles, geometry, hotkeys_mac, keyplan, launchd, power
from glance.platform.macos.ax_text import (
    py_to_utf16,
    range_to_py,
    range_to_utf16,
    utf16_len,
    utf16_to_py,
)
from glance.platform.macos.daemon import CachedMacConfig, capture_hello_filter
from glance.platform.macos.notify import applescript_string, notification_script
from glance.privacy.gate import FieldMeta, PrivacyGate
from glance.protocol import Rect

# ---------------------------------------------------------------------- geometry


def test_to_cocoa_flips_y_against_the_primary_height():
    # a 100x20 rect 30 px below the top of a 1080 px primary screen
    assert geometry.to_cocoa(Rect(10, 30, 100, 20), 1080) == (10.0, 1030.0, 100.0, 20.0)


def test_from_cocoa_is_the_inverse_of_to_cocoa():
    for r in (Rect(0, 0, 1, 1), Rect(-1920, 200, 640, 480), Rect(100, 1060, 50, 20)):
        assert geometry.from_cocoa(*geometry.to_cocoa(r, 1117), 1117) == r


def test_a_screen_above_the_primary_has_negative_top_left_y():
    # AppKit: a 1440x900 display placed above a 1920x1080 primary starts at y = 1080
    assert geometry.from_cocoa(0, 1080, 1440, 900, 1080) == Rect(0, -900, 1440, 900)


def test_point_from_cocoa():
    assert geometry.point_from_cocoa(15.4, 1000.0, 1080) == (15, 80)


def test_ax_rect_needs_a_positive_size():
    assert geometry.ax_rect((1.2, 2.6), (10, 20)) == Rect(1, 3, 10, 20)
    assert geometry.ax_rect((1, 2), (0, 20)) is None
    assert geometry.ax_rect(None, (5, 5)) is None


def test_caret_from_bounds_widens_a_zero_width_caret_and_drops_junk():
    screens = [Rect(0, 0, 1920, 1080)]
    assert geometry.caret_from_bounds((100, 200, 0, 17), screens) == Rect(100, 200, 1, 17)
    assert geometry.caret_from_bounds((0, 0, 0, 0), screens) is None
    assert geometry.caret_from_bounds((100, 200, 5, 0), screens) is None
    assert geometry.caret_from_bounds((5000, 200, 1, 17), screens) is None  # off every screen
    assert geometry.caret_from_bounds(None) is None


# ---------------------------------------------------------------------- UTF-16 offsets


def test_utf16_offsets_count_astral_characters_twice():
    text = "a😀b"
    assert utf16_len(text) == 4
    assert [utf16_to_py(text, u) for u in range(5)] == [0, 1, 2, 2, 3]
    assert [py_to_utf16(text, i) for i in range(4)] == [0, 1, 3, 4]


def test_ranges_round_trip():
    text = "héllo 😀 wörld"
    for start, end in ((0, 0), (1, 4), (6, 7), (8, 13), (0, len(text))):
        loc, length = range_to_utf16(text, start, end)
        assert range_to_py(text, loc, length) == (start, end)


def test_offsets_clamp():
    assert utf16_to_py("abc", -3) == 0
    assert utf16_to_py("abc", 99) == 3
    assert py_to_utf16("abc", 99) == 3


# ---------------------------------------------------------------------- roles and the gate


def _meta(**kw) -> FieldMeta:
    base = {"app": "TextEdit", "wm_class": "com.apple.TextEdit", "title": "Untitled", "url": None,
                "role": "text", "states": frozenset({"editable", "focused"}), "pid": 42, "text_len": 10}
    base.update(kw)
    return FieldMeta(**base)


def test_a_secure_text_field_is_a_password_for_the_gate():
    assert ax_roles.is_secure("AXTextField", "AXSecureTextField")
    assert ax_roles.is_secure("AXSecureTextField", None)
    role = ax_roles.role_name("AXTextField", "AXSecureTextField")
    gate = PrivacyGate(ax_roles.mac_config(Config()))
    st = ax_roles.states(editable=False, secure=True)
    assert "protected" in st
    assert gate.evaluate(_meta(role=role, states=st)).reason == "password"


def test_role_names():
    assert ax_roles.role_name("AXTextArea", None) == "text"
    assert ax_roles.role_name("AXTextField", None) == "entry"
    assert ax_roles.role_name("AXTextField", "AXSearchField") == "entry"
    assert ax_roles.role_name("AXGroup", None) == "text"  # contenteditable in a web area
    assert ax_roles.role_name("AXButton", None) == "AXButton"


def test_text_roles():
    assert ax_roles.is_text_role("AXTextArea", None, False)
    assert ax_roles.is_text_role("AXTextField", "AXSecureTextField", False)
    assert not ax_roles.is_text_role("AXGroup", None, False)
    assert ax_roles.is_text_role("AXGroup", None, True)
    assert not ax_roles.is_text_role("AXButton", None, True)


def test_states():
    assert "editable" in ax_roles.states(editable=True, secure=False)
    assert "editable" not in ax_roles.states(editable=False, secure=False)
    assert "multi-line" in ax_roles.states(editable=True, secure=False, multi_line=True)


@pytest.mark.parametrize("bundle", [
    "com.1password.1password", "com.bitwarden.desktop", "org.keepassxc.keepassxc",
    "com.apple.keychainaccess", "com.apple.ScreenSharing", "com.microsoft.rdc.macos",
    "com.apple.loginwindow",
])
def test_mac_denylist(bundle):
    gate = PrivacyGate(ax_roles.mac_config(Config()))
    assert gate.evaluate(_meta(wm_class=bundle, app="X")).reason == "app_denylist"


@pytest.mark.parametrize("bundle", ["com.apple.Terminal", "com.googlecode.iterm2"])
def test_mac_terminals_are_gated(bundle):
    gate = PrivacyGate(ax_roles.mac_config(Config()))
    assert gate.evaluate(_meta(wm_class=bundle, app="Terminal")).reason == "terminal"


@pytest.mark.parametrize("bundle", ["com.apple.Safari", "com.google.Chrome",
                                    "company.thebrowser.Browser", "com.brave.Browser"])
def test_a_browser_field_without_a_url_is_gated(bundle):
    gate = PrivacyGate(ax_roles.mac_config(Config()))
    assert gate.evaluate(_meta(wm_class=bundle, app="Browser")).reason == "url_unknown"
    ok = gate.evaluate(_meta(wm_class=bundle, app="Browser", url="https://example.com/x"))
    assert ok.allowed
    bank = gate.evaluate(_meta(wm_class=bundle, app="B", url="https://mybank.example/"))
    assert bank.reason == "url_denylist"


def test_incognito_title_is_gated_on_mac():
    gate = PrivacyGate(ax_roles.mac_config(Config()))
    m = _meta(wm_class="com.google.Chrome", app="Chrome", url="https://e.com",
              title="New Tab - Google Chrome (Incognito)")
    assert gate.evaluate(m).reason == "incognito"


def test_textedit_is_allowed():
    assert PrivacyGate(ax_roles.mac_config(Config())).evaluate(_meta()).allowed


def test_mac_config_only_adds():
    cfg = Config()
    mac = ax_roles.mac_config(cfg)
    assert set(cfg.privacy.app_denylist) <= set(mac.privacy.app_denylist)
    assert len(mac.privacy.app_denylist) == len({x.casefold()
                                                    for x in mac.privacy.app_denylist})
    assert ax_roles.mac_config(mac).privacy == mac.privacy  # idempotent


def test_manual_accessibility_attribute():
    assert ax_roles.wants_manual_accessibility("com.google.Chrome") == "AXEnhancedUserInterface"
    assert ax_roles.wants_manual_accessibility("com.tinyspeck.slackmacgap") == \
        "AXManualAccessibility"
    assert ax_roles.wants_manual_accessibility("com.apple.Safari") is None


def test_cached_mac_config_rebuilds_only_for_a_new_config():
    holder = [Config()]
    cached = CachedMacConfig(lambda: holder[0])
    first = cached()
    assert cached() is first
    holder[0] = Config()
    assert cached() is not first


def test_capture_hello_filter_records_ax_trust_and_passes_everything_on():
    seen, got = {}, []
    f = capture_hello_filter(lambda c, t, p: got.append(t), seen)
    f(None, "hello", {"role": "capture", "ax_trusted": False})
    f(None, "snapshot", {"ax_trusted": True})
    assert seen == {"capture_ax_trusted": False}
    assert got == ["hello", "snapshot"]


# ---------------------------------------------------------------------- key planning


def test_paste_and_undo_are_command_shortcuts():
    v = keyplan.plan_chord("ctrl", "v")
    assert (v.keycode, v.flags, v.chars) == (9, keyplan.COMMAND, "v")
    z = keyplan.plan_chord("ctrl", "z")
    assert (z.keycode, z.flags) == (6, keyplan.COMMAND)


def test_navigation_keeps_its_modifiers():
    s = keyplan.plan_chord("shift", "Left")
    assert (s.keycode, s.flags, s.chars) == (123, keyplan.SHIFT, "")
    b = keyplan.plan_chord("BackSpace")
    assert (b.keycode, b.flags) == (51, 0)
    ow = keyplan.plan_chord("alt", "shift", "Left")
    assert ow.flags == keyplan.OPTION | keyplan.SHIFT


def test_redo_is_shift_command_z():
    y = keyplan.plan_chord("ctrl", "y")
    assert (y.keycode, y.flags) == (6, keyplan.COMMAND | keyplan.SHIFT)


def test_bad_chords():
    with pytest.raises(ValueError):
        keyplan.plan_chord()
    with pytest.raises(ValueError):
        keyplan.plan_chord("hyper", "v")
    with pytest.raises(ValueError):
        keyplan.plan_chord("NoSuchKey")


def test_unicode_chunks_keep_characters_whole():
    assert keyplan.unicode_chunks("ab") == ["a", "b"]
    assert keyplan.unicode_chunks("éx") == ["é", "x"]
    assert keyplan.unicode_chunks("😀!") == ["😀", "!"]
    family = "👩‍👩‍👧"
    assert keyplan.unicode_chunks(family + "a") == [family, "a"]
    assert "".join(keyplan.unicode_chunks("héllo wörld 😀")) == "héllo wörld 😀"


# ---------------------------------------------------------------------- hotkeys


def test_linux_defaults_become_control_option_chords():
    mac = hotkeys_mac.mac_hotkeys(dict(hotkeys_mac.LINUX_DEFAULTS))
    assert mac == hotkeys_mac.MAC_DEFAULTS
    assert all(c.startswith("Ctrl+Alt+") for c in mac.values())


def test_the_config_defaults_are_the_linux_defaults_the_mac_replaces():
    assert Config().general.hotkeys == hotkeys_mac.LINUX_DEFAULTS


def test_a_users_own_chord_is_kept():
    mac = hotkeys_mac.mac_hotkeys({"panel": "Super+Shift+k", "pause": "Super+Alt+p"})
    assert mac == {"panel": "Super+Shift+k", "pause": "Ctrl+Alt+p"}


def test_carbon_codes():
    assert hotkeys_mac.carbon_chord("Ctrl+Alt+g") == (5, hotkeys_mac.CONTROL_KEY
                                                     | hotkeys_mac.OPTION_KEY)
    assert hotkeys_mac.carbon_chord("Alt+Return") == (36, hotkeys_mac.OPTION_KEY)
    assert hotkeys_mac.carbon_chord("Escape") == (53, 0)
    assert hotkeys_mac.carbon_chord("Tab") == (48, 0)
    assert hotkeys_mac.carbon_chord("Super+Shift+1") == (18, hotkeys_mac.CMD_KEY
                                                        | hotkeys_mac.SHIFT_KEY)
    with pytest.raises(ValueError):
        hotkeys_mac.carbon_chord("Ctrl+Alt+nosuchkey")


def test_every_default_and_transient_chord_has_a_carbon_code():
    from glance.daemon.links import ESCAPE, USE_KEY
    from glance.features.autocomplete import AC_CHORDS

    for chord in (*hotkeys_mac.MAC_DEFAULTS.values(), ESCAPE, USE_KEY, *AC_CHORDS.values()):
        hotkeys_mac.carbon_chord(chord)


def test_autocomplete_word_key_is_option_right_on_a_mac():
    from glance.features.autocomplete import AC_CHORDS, AC_WORD

    assert hotkeys_mac.mac_chord(AC_CHORDS[AC_WORD]) == "Alt+Right"
    assert hotkeys_mac.carbon_chord(hotkeys_mac.mac_chord(AC_CHORDS[AC_WORD])) == (
        124, hotkeys_mac.OPTION_KEY)
    assert hotkeys_mac.mac_chord("Tab") == "Tab"


def test_symbols():
    assert hotkeys_mac.symbols("Alt+Return") == "⌥⏎"
    assert hotkeys_mac.symbols("Ctrl+Alt+g") == "⌃⌥G"
    assert hotkeys_mac.symbols("Super+Shift+space") == "⇧⌘Space"
    assert hotkeys_mac.symbols("not a chord+") == "not a chord+"


def _symbolic(entries: dict) -> bytes:
    return plistlib.dumps({"AppleSymbolicHotKeys": entries})


def test_parse_symbolic_hotkeys_keeps_only_enabled_bound_ones():
    data = _symbolic({
        "60": {"enabled": True, "value": {"parameters": [32, 49, 262144], "type": "standard"}},
        "61": {"enabled": True, "value": {"parameters": [32, 49, 786432], "type": "standard"}},
        "64": {"enabled": False, "value": {"parameters": [32, 49, 1048576]}},
        "99": {"enabled": True, "value": {"parameters": [65535, 65535, 0]}},
        "bad": "nonsense",
    })
    got = hotkeys_mac.parse_symbolic_hotkeys(data)
    assert (frozenset({"ctrl"}), "space") in got
    assert (frozenset({"ctrl", "alt"}), "space") in got  # next input source
    assert (frozenset({"super"}), "space") not in got  # disabled Spotlight
    assert len(got) == 2


def test_parse_symbolic_hotkeys_garbage():
    assert hotkeys_mac.parse_symbolic_hotkeys(b"not a plist") == set()
    assert hotkeys_mac.parse_symbolic_hotkeys(plistlib.dumps([1, 2])) == set()


def test_a_system_shortcut_is_reported_not_grabbed():
    from glance.ui.hotkeys import bind_hotkeys

    class Port:
        def __init__(self):
            self.held = {}

        def grab(self, name, chord, cb):
            self.held[name] = chord
            return True

        def ungrab(self, name):
            self.held.pop(name, None)

        def grabbed(self):
            return dict(self.held)

    system = hotkeys_mac.parse_symbolic_hotkeys(_symbolic({
        "1": {"enabled": True, "value": {"parameters": [103, 5, 786432]}}}))  # ⌃⌥G
    port = Port()
    problems = bind_hotkeys(port, hotkeys_mac.MAC_DEFAULTS,
                            {n: (lambda: None) for n in hotkeys_mac.MAC_DEFAULTS}, system)
    assert problems == {"check": "system"}
    assert "check" not in port.held and "pause" in port.held


# ---------------------------------------------------------------------- launchd


def test_launch_agent_plist(tmp_path):
    home = Path("/Users/someone")
    glanced = Path("/Users/someone/.local/share/glance-src/.venv/bin/glanced")
    doc = plistlib.loads(launchd.render_plist(glanced, home, launchd.default_path_env(home)))
    assert doc["Label"] == "io.github.btocode.glance"
    assert doc["ProgramArguments"] == [str(glanced)]
    assert doc["RunAtLoad"] is True
    assert doc["KeepAlive"] == {"SuccessfulExit": False}
    assert doc["LimitLoadToSessionType"] == "Aqua"
    assert doc["ExitTimeOut"] == 5  # a hung quit never holds the logout
    assert doc["StandardOutPath"] == "/Users/someone/Library/Logs/Glance/glanced.log"
    assert doc["EnvironmentVariables"]["PATH"].startswith("/Users/someone/.local/bin:")
    assert launchd.agent_path(home) == home / "Library/LaunchAgents/io.github.btocode.glance.plist"


def test_launch_agent_plist_is_deterministic():
    home = Path("/Users/a")
    a = launchd.render_plist(Path("/x/glanced"), home, "/bin")
    assert a == launchd.render_plist(Path("/x/glanced"), home, "/bin")


def test_launchctl_argv():
    assert launchd.bootstrap_argv(501, Path("/p.plist")) == \
        ["launchctl", "bootstrap", "gui/501", "/p.plist"]
    assert launchd.bootout_argv(501) == ["launchctl", "bootout", "gui/501/io.github.btocode.glance"]
    assert launchd.kickstart_argv(501)[:3] == ["launchctl", "kickstart", "-k"]


def test_parse_launchctl_print():
    out = """gui/501/io.github.btocode.glance = {
\tactive count = 1
\tstate = running
\tprogram = /x/glanced
\tpid = 4242
\tlast exit code = (never exited)
\tjob state = running
}"""
    assert launchd.parse_print(out) == {"state": "running", "pid": "4242",
                                         "last exit code": "(never exited)"}


# ---------------------------------------------------------------------- power, notify


def test_pmset():
    assert power.parse_pmset("Now drawing from 'Battery Power'\n -InternalBattery-0 80%")
    assert not power.parse_pmset("Now drawing from 'AC Power'\n")
    assert not power.parse_pmset("")
    assert power.on_battery(lambda: "Now drawing from 'Battery Power'")


def test_applescript_strings_are_escaped():
    assert applescript_string('say "hi" \\ there') == '"say \\"hi\\" \\\\ there"'
    script = notification_script('Glance: "x"', "line")
    assert script == 'display notification "line" with title "Glance: \\"x\\""'


def test_runtime_dir_on_macos_is_under_the_state_dir(monkeypatch, tmp_path):
    import sys

    from glance import paths

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    d = paths.runtime_dir()
    assert d == tmp_path / "state" / "glance" / "run"
    assert d.stat().st_mode & 0o777 == 0o700


def test_runtime_dir_on_linux_is_unchanged(monkeypatch, tmp_path):
    from glance import paths

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert paths.runtime_dir() == tmp_path / "glance"
