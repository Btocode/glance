"""`[general] check = "shortcut"`: nothing is checked while typing; the check hotkey (or a
click on the status icon) checks the field and opens the strip. Plus the default chords,
the tray toggle and the extension schema (the icon's toolbar: test_icon_toolbar)."""
from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

from glance.backends.base import Analysis, BackendError
from glance.config import CHECK_MODES, Config, GeneralCfg, load_config, update_config
from glance.daemon.controller import OPEN_WAIT_MS
from glance.daemon.wiring import check_label, set_check_mode
from glance.grammar.corrections import strip_replacement
from glance.ui.hotkeys import parse_chord
from glance.ui.shortcut_table import LINUX_RESERVED, MAC_RESERVED
from glance.ui.status_model import StatusState
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


def test_strip_opens_at_once_with_a_loader_then_the_analysis(tmp_path):
    r = AiRig(tmp_path, analysis=TEH_AI, delay_ms=1500)
    r.c.handle_snapshot(ai_t.snap())
    r.c.check_now()
    r.clock.advance(100)
    strip = r.overlay.strip
    assert strip is not None and strip.loading == "Checking with Claude…"
    assert not strip.can_use  # the keycap waits for the AI
    assert r.overlay.status.state is StatusState.WORKING
    r.clock.advance(1500)
    strip = r.overlay.strip
    assert strip is not None and strip.loading is None and strip.can_use


def test_slow_analysis_keeps_the_loader_until_it_is_back(tmp_path):
    r = AiRig(tmp_path, analysis=TEH_AI, delay_ms=OPEN_WAIT_MS + 3000)
    r.c.handle_snapshot(ai_t.snap())
    r.c.check_now()
    r.clock.advance(OPEN_WAIT_MS + 10)
    assert r.overlay.strip is not None and r.overlay.strip.loading is not None
    r.clock.advance(3000)
    assert r.overlay.strip.loading is None


# ------------------------------------------------- the user's sentence (AI-first accuracy)

USER = "hey how are yo doing , seem sal alright , how to get  in place"
# What Harper says about it, as in the user's report: spacing, and "sal" ("Sal", "say").
USER_HARPER = {" ,": ("Spacing", (",",)), "  ": ("Spacing", (" ",)),
               "sal": ("SpellCheck", ("Sal", "say"))}
USER_AI = Analysis("casual", False, None, (), corrected=(
    "Hey how are you doing, seems sal alright, how to get in place"))


def _user_rig(tmp_path, analysis=USER_AI, delay_ms=2000):
    r = AiRig(tmp_path, analysis=analysis, patterns=USER_HARPER, delay_ms=delay_ms)
    r.c.handle_snapshot(ai_t.snap(USER))
    r.c.check_now()
    r.clock.advance(50)
    return r


def _shown(r):
    return "".join(seg.text for seg in r.overlay.strip.segments)


def _fix_list(r):
    """The drawer's one-line fix list: flagged -> first replacement."""
    return [(row.flagged, row.replacements[0]) for row in r.overlay.panel.issues]


def _applied(text, issues):
    out, pos = [], 0
    for i in sorted(issues, key=lambda i: i.start):
        out += [text[pos:i.start], i.replacements[0]]
        pos = i.end
    return "".join(out) + text[pos:]


def test_user_sentence_ai_replaces_the_local_preview(tmp_path):
    r = _user_rig(tmp_path)
    r.c.toggle_panel()
    # At once: the quick local fixes as a preview, the loader, the drawer's skeletons.
    assert r.overlay.strip.loading == "Checking with Claude…"
    preview = _shown(r)
    assert "doing," in preview and "get in" in preview
    assert "Sal" not in preview  # a capitalised guess at a name is not a confident fix
    assert r.overlay.panel.loading
    r.clock.advance(2000)
    # The AI's corrected text replaced it, in place.
    assert r.overlay.strip.loading is None and not r.overlay.panel.loading
    assert _shown(r) == "Hey how are you doing, seems sal alright, how to get in place"
    # ... and the drawer lists exactly the changes the strip shows, nothing else.
    assert all(i.source == "llm" for i in r.c.issues)
    assert _applied(USER, r.c.issues) == _shown(r)
    fixes = _fix_list(r)
    assert ("hey", "Hey") in fixes and ("yo", "you") in fixes and ("seem", "seems") in fixes
    assert ("doing ,", "doing,") in fixes and ("get  in", "get in") in fixes
    assert not any("say" in rep or "Sal" in rep for _f, rep in fixes)
    shown = _shown(r)
    r.c.use_strip()
    (apply,) = r.c._capture.applies
    assert apply.edit.replacement == shown


def test_user_sentence_with_the_reported_style_of_answer(tmp_path):
    """The answer style from the report: punctuation, case, agreement and a missing word,
    all minimal edits; the strip shows exactly it and the fix list lists exactly it."""
    answer = "Hey, how are you doing? Seems all right — how do we get it in place?"
    r = _user_rig(tmp_path, analysis=Analysis("casual", False, None, (), corrected=answer),
                  delay_ms=0)
    r.c.toggle_panel()
    assert _shown(r) == answer
    assert all(i.source == "llm" for i in r.c.issues)
    assert _applied(USER, r.c.issues) == answer
    fixes = _fix_list(r)
    assert ("hey", "Hey,") in fixes and ("yo", "you") in fixes and ("doing ,", "doing?") in fixes
    assert ("seem", "Seems") in fixes
    assert not any(rep in ("Sal", "say") for _f, rep in fixes)


def test_user_sentence_strip_and_panel_agree_without_the_ai(tmp_path):
    r = _user_rig(tmp_path, analysis=None, delay_ms=0)
    r.backend.script(BackendError("timeout"))
    r.c.handle_snapshot(ai_t.snap(USER + " "))
    r.c.check_now()
    r.clock.advance(50)
    r.c.toggle_panel()
    strip = r.overlay.strip
    assert strip.loading is None and strip.can_use
    assert strip.meta == "AI check timed out — showing quick fixes"
    fixes = _fix_list(r)
    assert ("sal", "say") not in fixes  # the list never disagrees with the strip
    assert _applied(r.c.current.text, [i for i in r.c.issues
                                       if strip_replacement(r.c.current.text, i)]) \
        .strip() == _shown(r)


def test_ai_corrections_keep_only_minimal_edits_and_protected_text(tmp_path):
    rewrite = Analysis("casual", False, None, (), corrected=(
        "Hello there! I hope you are doing well; it all seems fine. How can we set it up?"))
    r = _user_rig(tmp_path, analysis=rewrite, delay_ms=0)
    assert not any(i.source == "llm" for i in r.c.issues)  # a rewrite is not a correction
    assert "doing," in _shown(r)  # the quick fixes stay


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
