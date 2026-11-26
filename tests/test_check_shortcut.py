"""`[general] check = "shortcut"`: nothing is checked while typing; the check hotkey (or a
click on the status icon) checks the field and opens the strip. Plus the default chords,
the tray toggle, the extension schema and the hover menu model."""
from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

from glance.backends.base import Analysis
from glance.config import CHECK_MODES, Config, GeneralCfg, load_config, update_config
from glance.daemon.controller import OPEN_WAIT_MS
from glance.daemon.panel_actions import PanelRouter
from glance.daemon.wiring import check_label, set_check_mode
from glance.ui.hotkeys import parse_chord
from glance.ui.hover_menu import (
    CHECK,
    RESUME,
    SETUP,
    TYPING_QUIET_MS,
    hover_menu,
    may_open,
    menu_piece,
)
from glance.ui.shortcut_table import LINUX_RESERVED, MAC_RESERVED
from glance.ui.status_model import NO_AI_LINE, PAUSED_LINE, PRIVATE_LINE, StatusState, StatusView
from glance.ui.tray_gtk import check_mode_labels
from tests import test_ai_analyze as ai_t
from tests import test_controller as ct

ROOT = Path(__file__).resolve().parents[1]
SHORTCUT = Config()  # the default


class Rig(ct.Rig):
    def __init__(self, tmp_path, cfg=SHORTCUT, **kw):
        super().__init__(tmp_path, **kw)
        self.cfg = cfg
        self.c.refresh()  # the controller was built in "typing" mode: switch it


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def checked(rig):
    rig.c.check_now()
    rig.clock.advance(0)


# ---------------------------------------------------------------- config


def test_default_is_shortcut_and_typos_fall_back(tmp_path):
    assert GeneralCfg().check == "shortcut" and CHECK_MODES == ("shortcut", "typing")
    p = tmp_path / "config.toml"
    p.write_text('[general]\ncheck = "typing"\n')
    assert load_config(p).general.check == "typing"
    p.write_text('[general]\ncheck = "sometimes"\n')
    assert load_config(p).general.check == "shortcut"


def test_tray_toggle_writes_config_and_reloads(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("# mine\n[ui]\nsuggestions = \"auto\"\n")
    reloads = []
    set_check_mode(update_config, p, "typing", lambda: reloads.append(1))
    assert load_config(p).general.check == "typing" and reloads == [1]
    assert "# mine" in p.read_text()
    set_check_mode(update_config, p, "shortcut", lambda: reloads.append(2))
    assert load_config(p).general.check == "shortcut" and reloads == [1, 2]
    with pytest.raises(ValueError):
        set_check_mode(update_config, p, "never", lambda: None)
    assert check_mode_labels("Super+Alt+G") == (
        ("shortcut", "Check: on shortcut (Super+Alt+G)"), ("typing", "Check: while typing"))


# ---------------------------------------------------------------- shortcut mode


def test_typing_checks_nothing(rig):
    rig.c.handle_snapshot(ct.snap())
    rig.clock.advance(5000)
    assert rig.harper.calls == [] and rig.c.issues == ()
    assert rig.overlay.strip is None
    assert rig.overlay.status.state is StatusState.IDLE  # quiet: ready to check


def test_check_now_checks_and_opens_the_strip(rig):
    rig.c.handle_snapshot(ct.snap())
    checked(rig)
    assert rig.harper.calls == [ct.TEH]
    assert [rig.c.current.text[i.start:i.end] for i in rig.c.issues] == ["Teh"]
    assert rig.overlay.strip is not None and rig.overlay.strip.can_use
    assert rig.overlay.status.state is StatusState.READY


def test_icon_shows_working_until_the_check_is_back(tmp_path):
    r = Rig(tmp_path, deferred=True)
    r.c.handle_snapshot(ct.snap())
    r.c.check_now()
    assert r.overlay.status.state is StatusState.WORKING and r.overlay.strip is None
    r.deliver()
    assert r.overlay.strip is not None


def test_edit_after_a_check_closes_the_strip(rig):
    rig.c.handle_snapshot(ct.snap())
    checked(rig)
    rig.c.handle_snapshot(ct.snap(ct.TEH + " over", seq=2))
    assert rig.overlay.strip is None and rig.c.issues == ()
    assert rig.overlay.status.state is StatusState.IDLE
    rig.clock.advance(5000)
    assert rig.harper.calls == [ct.TEH]  # nothing more was checked


def test_caret_move_keeps_the_strip(rig):
    s = ct.snap()
    rig.c.handle_snapshot(s)
    checked(rig)
    rig.c.handle_snapshot(replace(s, caret=1, seq=2))
    assert rig.overlay.strip is not None


def test_pressing_again_rechecks(rig):
    rig.c.handle_snapshot(ct.snap())
    checked(rig)
    checked(rig)
    assert rig.harper.calls == [ct.TEH, ct.TEH] and rig.overlay.strip is not None


def test_escape_and_focus_change_close(rig):
    rig.c.handle_snapshot(ct.snap())
    checked(rig)
    rig.c.close_panel(snooze=True)
    assert rig.overlay.strip is None
    checked(rig)
    rig.c.handle_snapshot(ct.snap(field_id="f2"))
    assert rig.overlay.strip is None and rig.c.issues == ()


def test_icon_click_checks(rig):
    rig.c.handle_snapshot(ct.snap())
    rig.c.toggle_strip()
    rig.clock.advance(0)
    assert rig.harper.calls == [ct.TEH] and rig.overlay.strip is not None
    rig.c.toggle_strip()  # open: a click closes it
    assert rig.overlay.strip is None


def test_short_text_is_checked_too(rig):
    rig.c.handle_snapshot(ct.snap("Teh"))
    checked(rig)
    assert rig.overlay.strip is not None and rig.overlay.strip.can_use


def test_typing_mode_is_unchanged(tmp_path):
    r = Rig(tmp_path, cfg=replace(SHORTCUT, general=replace(GeneralCfg(), check="typing")))
    r.c.handle_snapshot(ct.snap())
    r.clock.advance(400)
    assert r.harper.calls == [ct.TEH] and r.c.issues
    assert r.overlay.strip is None  # on_click: the icon opens it


def test_switching_modes_live(rig):
    rig.c.handle_snapshot(ct.snap())
    rig.cfg = replace(SHORTCUT, general=replace(GeneralCfg(), check="typing"))
    rig.c.refresh()
    rig.clock.advance(400)
    assert rig.harper.calls == [ct.TEH] and rig.c.issues
    rig.cfg = SHORTCUT
    rig.c.refresh()
    assert rig.c.issues == ()


# ---------------------------------------------------------------- with the AI


class AiRig(ai_t.Rig):
    def __init__(self, tmp_path, **kw):
        super().__init__(tmp_path, **kw)
        self.cfg = replace(self.cfg, general=GeneralCfg())  # shortcut
        self.c.refresh()


TEH_AI = Analysis("neutral", False, None, (ai_t.TEH,))


def test_no_ai_call_while_typing_one_on_the_shortcut(tmp_path):
    r = AiRig(tmp_path, analysis=TEH_AI)
    r.c.handle_snapshot(ai_t.snap())
    r.clock.advance(5000)
    assert [c.kind for c in r.backend.calls] == []
    r.c.check_now()
    r.clock.advance(0)
    assert [c.kind for c in r.backend.calls] == ["analyze"]
    r.clock.advance(10_000)
    assert [c.kind for c in r.backend.calls] == ["analyze"]  # nothing speculative after


def test_strip_waits_for_the_analysis(tmp_path):
    r = AiRig(tmp_path, analysis=TEH_AI, delay_ms=1500)
    r.c.handle_snapshot(ai_t.snap())
    r.c.check_now()
    r.clock.advance(100)
    assert r.overlay.strip is None and r.overlay.status.state is StatusState.WORKING
    r.clock.advance(1500)
    assert r.overlay.strip is not None


def test_slow_analysis_opens_with_what_there_is(tmp_path):
    r = AiRig(tmp_path, analysis=TEH_AI, delay_ms=OPEN_WAIT_MS + 3000)
    r.c.handle_snapshot(ai_t.snap())
    r.c.check_now()
    r.clock.advance(OPEN_WAIT_MS + 10)
    assert r.overlay.strip is not None


# ---------------------------------------------------------------- hover menu


def view(state, message=None):
    return StatusView(state, message=message)


def test_hover_menu_items_per_state():
    full = hover_menu(view(StatusState.IDLE), "Super+Alt+G")
    assert [i.label for i in full.items] == ["Check spelling & grammar", "Check tone",
                                             "Rewrite…", "Shorter", "More professional"]
    assert full.hint == "Super+Alt+G to check" and full.note is None
    assert full.items[3].args == {"key": "shorter"}
    paused = hover_menu(view(StatusState.MUTED, PAUSED_LINE), "Super+Alt+G")
    assert paused.items == (RESUME,) and paused.note == PAUSED_LINE and paused.hint is None
    private = hover_menu(view(StatusState.MUTED, PRIVATE_LINE))
    assert private.items == () and private.note == PRIVATE_LINE
    no_ai = hover_menu(view(StatusState.MUTED, NO_AI_LINE), "⌃⌥G")
    assert no_ai.items == (CHECK, SETUP) and no_ai.hint == "⌃⌥G to check"
    assert hover_menu(view(StatusState.HIDDEN)) is None
    assert menu_piece(no_ai)["items"][1] == {"label": "Connect an AI…", "action": "setup",
                                             "args": {}}


def test_typing_suppresses_hover_open(rig):
    assert may_open(10.0, None) and may_open(10.0, 10.0 - TYPING_QUIET_MS / 1000)
    assert not may_open(10.0, 9.9)
    rig.c.handle_snapshot(ct.snap())
    rig.clock.advance(1000)
    rig.c.handle_snapshot(ct.snap(ct.TEH + " o", seq=2))
    assert rig.c.hover_menu("Super+Alt+G") is None  # a key just now
    rig.clock.advance(TYPING_QUIET_MS)
    assert rig.c.hover_menu("Super+Alt+G").items[0] == CHECK


def test_hover_menu_actions_route(rig):
    opened = []
    router = PanelRouter(rig.c, on_setup=lambda: opened.append(1))
    rig.c.handle_snapshot(ct.snap())
    router.action("check", {})
    rig.clock.advance(0)
    assert rig.overlay.strip is not None
    router.action("setup", {})
    assert opened == [1]
    rig.c.close_panel()
    router.action("check_tone", {})
    rig.clock.advance(0)
    assert rig.overlay.strip is not None and rig.overlay.panel is not None


def test_check_label():
    assert check_label("Super+Alt+g") == "Super+Alt+G"
    assert check_label("Ctrl+Alt+g", mac=True) == "⌃⌥G"


# ---------------------------------------------------------------- default chords


def _key(chord):
    mods, key = parse_chord(chord)
    return mods, key.casefold()


def test_default_chords_are_unique_and_off_the_desktops_shortcuts():
    from glance.platform.macos.hotkeys_mac import MAC_DEFAULTS, mac_hotkeys

    linux = GeneralCfg().hotkeys
    assert linux["check"] == "Super+Alt+g"
    assert len({_key(c) for c in linux.values()}) == len(linux)
    reserved = {_key(c) for c, _owner in LINUX_RESERVED}
    assert not [c for c in linux.values() if _key(c) in reserved]
    mac = mac_hotkeys(linux)
    assert mac == MAC_DEFAULTS and mac["check"] == "Ctrl+Alt+g"
    assert len({_key(c) for c in mac.values()}) == len(mac)
    reserved = {_key(c) for c, _owner in MAC_RESERVED}
    assert not [c for c in mac.values() if _key(c) in reserved]


def test_docs_list_every_default_chord():
    doc = (ROOT / "docs" / "shortcuts.md").read_text()
    for chord in GeneralCfg().hotkeys.values():
        assert chord in doc, chord
    assert "Super+Alt+s" in doc  # the screen reader: why selection is not there


def test_extension_schema_has_every_hotkey():
    xml = (ROOT / "extension" / "schemas" / "org.gnome.shell.extensions.glance.gschema.xml")
    keys = set(re.findall(r'<key name="([^"]+)"', xml.read_text()))
    assert {n.replace("_", "-") for n in GeneralCfg().hotkeys} == keys
    js = (ROOT / "extension" / "extension.js").read_text()
    assert "'check'" in js.split("SCHEMA_KEYS")[1].split(";")[0]


def test_extension_hand_cursor_works_on_every_gnome():
    # GNOME 48+ has no Meta.Cursor (reading it threw in every hover handler on GNOME 50):
    # there each clickable actor carries its own cursor; Meta.Cursor is read only in a try.
    js = (ROOT / "extension" / "overlay.js").read_text()
    assert "actor.set_cursor_type(Clutter.CursorType.POINTER)" in js
    set_cursor = js[js.index("function setCursor(hand) {"):]
    set_cursor = set_cursor[:set_cursor.index("\n}\n")]
    assert set_cursor.index("try {") < set_cursor.index("Meta.Cursor")
    assert js.count("Meta.Cursor") == set_cursor.count("Meta.Cursor") + 1  # + one comment


# ---------------------------------------------------------------- doctor


def test_doctor_names_the_check_chord_and_live_conflicts(tmp_path, monkeypatch):
    from glance.doctor.checks import Hotkeys
    from tests import test_doctor as td

    home = tmp_path / "home"
    (home / ".config" / "glance").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    env = td.make_env(home)
    r = Hotkeys().run(env)
    assert r.status == "ok" and "Super+Alt+g" in r.detail

    def run(argv, timeout=None):  # a desktop that took Super+Alt+G for itself
        if argv[:2] == ["gsettings", "list-recursively"] and "media-keys" in argv[2]:
            return 0, "org.gnome.settings-daemon.plugins.media-keys custom ['<Super><Alt>g']\n"
        return 0, ""

    r = Hotkeys().run(td.make_env(home, run=run))
    assert r.status == "warn" and "check (Super+Alt+g)" in r.detail

    status = {"ok": True, "hotkeys_grabbed": "panel", "hotkeys_problems": "check:conflict"}
    r = Hotkeys().run(td.make_env(home, ctl=lambda t, p: status))
    assert r.status == "warn" and "check (Super+Alt+g: another program holds it)" in r.detail


def test_the_hotkeys_own_blur_does_not_lose_the_check(rig):
    s = ct.snap()
    rig.c.handle_snapshot(s)
    checked(rig)  # key first, then the grab's blur
    rig.c.handle_focus_lost("blur")
    assert rig.overlay.strip is None
    rig.clock.advance(100)
    rig.c.handle_snapshot(replace(s, seq=2))
    rig.clock.advance(0)
    assert rig.overlay.strip is not None
    rig.c.close_panel()
    rig.c.handle_focus_lost("blur")  # the blur first, then the key
    rig.c.check_now()
    rig.clock.advance(100)
    rig.c.handle_snapshot(replace(s, seq=3))
    rig.clock.advance(0)
    assert rig.overlay.strip is not None


def test_shell_overlay_sends_the_hover_menu_in_the_scene():
    from glance.ui.overlay_shell import ShellOverlay
    from tests import test_shell_adapters as sa

    bus = sa.bus_with_monitors()
    posted, actions = [], []
    ov = ShellOverlay(bus, posted.append, lambda ms, fn: None, lambda: sa.LIGHT,
                      lambda: None, lambda action, args: actions.append((action, args)),
                      hover_menu=lambda: hover_menu(view(StatusState.IDLE), "Super+Alt+G"))

    def run():
        while posted:
            posted.pop(0)()

    ov.on_window({"reason": "focus", "focus": sa.WINDOW, "monitors": [[0, 0, 1280, 800]]})
    ov.show_status(view(StatusState.IDLE), sa.ANCHOR)
    run()
    assert "menu" not in sa.last_scene(bus)["status"]
    ov.on_clicked({"target": "hover", "args": {}})
    run()
    menu = sa.last_scene(bus)["status"]["menu"]
    assert menu["items"][0]["action"] == "check" and menu["hint"] == "Super+Alt+G to check"
    ov.on_clicked({"target": "action", "args": {"action": "check", "args": {}}})
    run()
    assert actions == [("check", {})] and "menu" not in sa.last_scene(bus)["status"]
    ov.on_clicked({"target": "hover", "args": {}})
    ov.on_clicked({"target": "hover_end", "args": {}})
    run()
    assert "menu" not in sa.last_scene(bus)["status"]
