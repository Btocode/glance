from glance.config import GeneralCfg
from glance.ui import look
from glance.ui.shortcut_table import GLANCE_ACTIONS, glance_shortcuts
from glance.ui.strip_model import Segment
from glance.ui.theme import DARK


def test_keycaps_name_each_key():
    assert look.chord_keys("Super+Alt+Return") == ["Super", "Alt", "Enter"]
    assert look.chord_keys("alt+super+space") == ["Super", "Alt", "Space"]
    assert look.chord_keys("Ctrl+Alt+g", mac=True) == ["⌃", "⌥", "G"]
    assert look.chord_keys("Not+A+Chord") == ["Not+A+Chord"]
    assert look.chord_text("Alt+Return") == "Alt+Enter"
    assert look.chord_text("Alt+Return", mac=True) == "⌥⏎"
    assert look.apply_label("Alt+Return") == "Apply (Alt+Enter)"


def test_panel_lines():
    assert look.subline("field", True) == "Local grammar · AI tones"
    assert look.subline("field", False) == "Local grammar"
    assert look.subline("selection", True) == "Selected text · AI tones"
    assert look.tone_line("Blunt", "curt") == "Reads blunt — curt"
    assert look.tone_line(None, "curt") is None
    assert look.fixes_label(1) == "1 fix" and look.fixes_label(6) == "6 fixes"


def test_every_tone_has_a_line_glyph_never_an_emoji():
    assert look.tone_glyph("friendlier") == "heart"
    assert look.tone_glyph("shorter") == "lines"
    assert look.tone_glyph("more_direct") == "bolt"
    assert look.tone_glyph("professional") == "briefcase"
    assert look.tone_glyph("custom") == "pen"
    assert look.tone_glyph("sharpen_prompt") == "spark"
    for name, path in look.GLYPHS.items():
        assert path[0][0] == "M", name
        for cmd, *pts in path:
            assert cmd in "MLCZ" and len(pts) == {"M": 2, "L": 2, "C": 6, "Z": 0}[cmd]
            assert all(0 <= p <= 16 for p in pts), name


def test_markup_escapes_the_users_text_and_marks_the_changes():
    segs = (Segment("a <b> ", False), Segment("&fix", True))
    assert look.strip_markup(segs, "#3ECF8E") == (
        'a &lt;b&gt; <span foreground="#3ECF8E" weight="600">&amp;fix</span>')
    assert look.suggested_markup(segs, DARK) == (
        f'a &lt;b&gt; <span background="{DARK["highlight"]}" '
        f'foreground="{DARK["text"]}">&amp;fix</span>')


def test_the_shortcuts_window_lists_the_configured_chords():
    hotkeys = {**GeneralCfg().hotkeys, "panel": "Super+Alt+o"}
    rows = dict((what, keys) for keys, what in glance_shortcuts(hotkeys, "Alt+Return"))
    assert len(rows) == len(GLANCE_ACTIONS)
    assert rows["Apply the suggestion"] == ["Alt", "Enter"]
    assert rows["Open the panel and tones"] == ["Super", "Alt", "O"]
    assert rows["Pick tone 1–4 (then Apply)"] == ["Super", "Alt", "1…4"]
    assert rows["Take the completion"] == ["Tab"]
    mac = dict((what, keys) for keys, what in glance_shortcuts(GeneralCfg().hotkeys,
                                                              mac=True))
    assert mac["Open the panel and tones"] == ["⌃", "⌥", "O"]
    assert mac["Apply the suggestion"] == ["⌥", "⏎"]
