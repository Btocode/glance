import pytest

from glance.ui.hotkeys import bind_hotkeys, parse_accels, parse_chord
from tests.fakes import FakeHotkeys


def test_parse_chord():
    assert parse_chord("Super+Alt+space") == (frozenset({"super", "alt"}), "space")
    with pytest.raises(ValueError):
        parse_chord("Hyper+x")


def test_parse_chord_bare_key_and_control_alias():
    assert parse_chord("Tab") == (frozenset(), "Tab")
    assert parse_chord("Control+Right") == (frozenset({"ctrl"}), "Right")
    assert parse_chord("SUPER+alt+F") == (frozenset({"super", "alt"}), "F")
    assert parse_chord(" Ctrl + Shift + z ") == (frozenset({"ctrl", "shift"}), "z")


@pytest.mark.parametrize("bad", ["", "Super+Alt+", "+", "Super++x", "Meta+x"])
def test_parse_chord_rejects(bad):
    with pytest.raises(ValueError):
        parse_chord(bad)


def test_bind_grabs_every_chord_that_has_an_action():
    port, fired = FakeHotkeys(), []
    chords = {"panel": "Super+Alt+space", "fix_all": "Super+Alt+f", "tone1": "Super+Alt+1"}
    actions = {"panel": lambda: fired.append("panel"), "fix_all": lambda: fired.append("fix")}
    assert bind_hotkeys(port, chords, actions) == {}
    assert port.grabbed() == {"panel": "Super+Alt+space", "fix_all": "Super+Alt+f"}
    port.grabs["fix_all"][1]()
    assert fired == ["fix"]


def test_bind_reports_conflicts_and_bad_chords_without_raising():
    port = FakeHotkeys(taken={"Super+Alt+f"})
    chords = {"panel": "Super+Alt+space", "fix_all": "Super+Alt+f", "pause": "Hyper+p",
              "revert": "Super+Alt+s"}
    actions = dict.fromkeys(chords, lambda: None)
    system = {(frozenset({"super", "alt"}), "s")}
    problems = bind_hotkeys(port, chords, actions, system)
    assert problems == {"fix_all": "conflict", "pause": "invalid", "revert": "system"}
    assert port.grabbed() == {"panel": "Super+Alt+space"}
    assert ("grab", "revert") not in port.log  # a system shortcut is never taken away


def test_rebind_releases_old_chords_but_not_foreign_grabs():
    port = FakeHotkeys()
    actions = {"panel": lambda: None, "pause": lambda: None}
    bind_hotkeys(port, {"panel": "Super+Alt+space", "pause": "Super+Alt+p"}, actions)
    port.grab("escape", "Escape", lambda: None)  # the panel's transient grab
    bind_hotkeys(port, {"panel": "Super+Alt+x"}, actions)
    assert port.grabbed() == {"panel": "Super+Alt+x", "escape": "Escape"}


def test_parse_accels_reads_gsettings_output():
    out = (
        "org.gnome.settings-daemon.plugins.media-keys screenreader ['<Alt><Super>s']\n"
        "org.gnome.settings-daemon.plugins.media-keys magnifier ['<Alt><Super>8']\n"
        "org.gnome.desktop.wm.keybindings switch-input-source ['<Super>space', 'XF86Keyboard']\n"
        "org.gnome.desktop.wm.keybindings close ['<Primary><Shift>Q']\n"
        "org.gnome.desktop.wm.keybindings unmaximize @as []\n"
        "org.gnome.mutter.keybindings odd ['<Hyper>x']\n"
    )
    assert parse_accels(out) == {
        (frozenset({"super", "alt"}), "s"),
        (frozenset({"super", "alt"}), "8"),
        (frozenset({"super"}), "space"),
        (frozenset({"ctrl", "shift"}), "q"),
        (frozenset(), "xf86keyboard"),
    }
