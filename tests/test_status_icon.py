"""The status icon: its state model, where it goes, and the on_click / auto strip setting."""
from __future__ import annotations

import pytest

from glance.config import Config, UiCfg, load_config, update_config
from glance.daemon.links import PanelEscape
from glance.daemon.wiring import set_suggestions
from glance.protocol import ApplyResult, Rect
from glance.ui.placement import Anchor, place_status
from glance.ui.status_model import (
    AI_LIMIT_LINE,
    CHECKING_LINE,
    NO_AI_LINE,
    NOTHING_LINE,
    PAUSED_LINE,
    PRIVATE_LINE,
    StatusInputs,
    StatusState,
    StatusView,
    status_view,
)
from glance.ui.strip_model import Segment, StripView
from tests.fakes import FakeHotkeys, FakeOverlay
from tests.test_controller import AUTO, TEH, Rig, snap

MON = [Rect(0, 0, 1920, 1080)]
ON = StatusInputs(has_field=True, ai="ok")


# ---------------------------------------------------------------- the state model


@pytest.mark.parametrize("inputs, want", [
    (StatusInputs(), StatusView(StatusState.HIDDEN)),
    (StatusInputs(paused=True), StatusView(StatusState.HIDDEN)),  # no field: nothing
    (ON, StatusView(StatusState.IDLE)),
    (StatusInputs(has_field=True, ai="off"), StatusView(StatusState.IDLE)),
    (StatusInputs(has_field=True, paused=True, checking=True, n_fixes=2),
     StatusView(StatusState.MUTED, message=PAUSED_LINE)),
    (StatusInputs(has_field=True, gated=True, n_fixes=2),
     StatusView(StatusState.MUTED, message=PRIVATE_LINE)),
    (StatusInputs(has_field=True, checking=True, n_fixes=3),
     StatusView(StatusState.WORKING, spin=True)),
    # keys still arriving: working, but a still ring (nothing redrawn per keystroke)
    (StatusInputs(has_field=True, checking=True, typing=True), StatusView(StatusState.WORKING)),
    (StatusInputs(has_field=True, analyzing=True), StatusView(StatusState.WORKING, spin=True)),
    (StatusInputs(has_field=True, analyzing=True, typing=True, ai="none"),
     StatusView(StatusState.WORKING, spin=True)),
    (StatusInputs(has_field=True, n_fixes=3), StatusView(StatusState.READY, 3)),
    (StatusInputs(has_field=True, has_note=True), StatusView(StatusState.READY)),
    (StatusInputs(has_field=True, n_fixes=1, ai="none"), StatusView(StatusState.READY, 1)),
    (StatusInputs(has_field=True, ai="none"), StatusView(StatusState.MUTED, message=NO_AI_LINE)),
    (StatusInputs(has_field=True, ai="limit"),
     StatusView(StatusState.MUTED, message=AI_LIMIT_LINE)),
])
def test_status_state_for_its_inputs(inputs, want):
    assert status_view(inputs) == want


def test_badge_is_the_count_a_dot_for_tone_only_and_nothing_otherwise():
    assert StatusView(StatusState.READY, 3).badge == "3"
    assert StatusView(StatusState.READY, 12).badge == "9+"
    assert StatusView(StatusState.READY).badge == "•"
    assert StatusView(StatusState.IDLE).badge is None
    assert StatusView(StatusState.WORKING, 3).badge is None


# ---------------------------------------------------------------- placement

FIELD = Rect(100, 100, 600, 200)


def test_icon_sits_inside_the_right_edge_on_the_caret_line():
    a = Anchor(caret=Rect(150, 140, 1, 20), field=FIELD)
    assert place_status(a, 22, MON) == (700 - 22 - 4, 150 - 11)


def test_icon_moves_outside_when_the_caret_reaches_it_or_text_follows():
    near = Anchor(caret=Rect(680, 140, 1, 20), field=FIELD)
    assert place_status(near, 22, MON) == (704, 139)
    tail = Anchor(caret=Rect(150, 140, 1, 20), field=FIELD, tail=True)
    assert place_status(tail, 22, MON) == (704, 139)


def test_icon_goes_above_the_line_without_room_outside():
    wide = Rect(100, 100, 1820, 200)  # reaches the monitor's right edge
    a = Anchor(caret=Rect(1900, 140, 1, 20), field=wide)
    x, y = place_status(a, 22, MON)
    assert (x, y) == (1920 - 22 - 4, 140 - 22 - 4)
    assert y + 22 <= 140  # never over the line being typed


def test_icon_falls_back_to_the_field_then_the_window():
    assert place_status(Anchor(field=FIELD), 22, MON) == (704, 104)
    assert place_status(Anchor(window=Rect(50, 50, 800, 600)), 22, MON) == (854, 54)
    assert place_status(Anchor(prev_char=Rect(150, 140, 8, 20), field=FIELD), 22, MON) == \
        (674, 139)
    assert place_status(Anchor(), 22, MON) is None


def test_icon_moves_with_the_anchor():
    a = Anchor(caret=Rect(150, 140, 1, 20), field=FIELD)
    x, y = place_status(a, 22, MON)
    assert place_status(a.moved(40, 30), 22, MON) == (x + 40, y + 30)


# ---------------------------------------------------------------- the controller


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path, deferred=True)


def _settle(rig, s=None):
    rig.c.handle_snapshot(s or snap())
    rig.clock.advance(400)
    while rig.bg:
        rig.deliver()


def test_icon_shows_on_focus_working_while_the_check_is_pending_then_ready(rig):
    rig.c.handle_snapshot(snap())
    # at once, before any check; still while keys may keep coming
    assert rig.overlay.status == StatusView(StatusState.WORKING)
    assert rig.overlay.status_anchor.field == Rect(90, 190, 400, 300)
    rig.clock.advance(400)
    assert rig.bg  # the check is in flight after the pause: the ring turns
    assert rig.overlay.status == StatusView(StatusState.WORKING, spin=True)
    rig.deliver()
    assert rig.overlay.status == StatusView(StatusState.READY, 1)
    assert rig.overlay.strip is None  # on_click: not shown by itself


def test_a_crashing_grammar_check_still_stops_the_ring(rig):
    def boom(_text):
        raise RuntimeError("harper went away")
    rig.harper.check = boom
    rig.c.handle_snapshot(snap())
    rig.clock.advance(400)
    assert rig.overlay.status == StatusView(StatusState.WORKING, spin=True)
    rig.deliver()
    assert rig.overlay.status is not None
    assert rig.overlay.status.state != StatusState.WORKING


def test_icon_is_idle_without_anything_to_fix_and_hides_on_blur(rig):
    _settle(rig, snap(text="The quick brown fox jumps over it."))
    assert rig.overlay.status == StatusView(StatusState.IDLE)
    rig.c.handle_focus_lost("blur")
    assert rig.overlay.status is None


def test_icon_working_while_an_ai_call_is_in_flight(rig):
    analyzing = {"analyzing": False}
    rig.c.extra_chip_inputs.append(lambda s: dict(analyzing))
    _settle(rig)
    assert rig.overlay.status.state is StatusState.READY
    analyzing["analyzing"] = True
    rig.c.refresh()
    assert rig.overlay.status == StatusView(StatusState.WORKING, spin=True)
    analyzing["analyzing"] = False
    rig.c.refresh()
    assert rig.overlay.status == StatusView(StatusState.READY, 1)


def test_icon_muted_when_paused_or_private_and_its_click_says_why(rig):
    _settle(rig)
    rig.c.toggle_pause()
    assert rig.overlay.status == StatusView(StatusState.MUTED, message=PAUSED_LINE)
    rig.c.toggle_strip()
    assert rig.overlay.strip == StripView(note=PAUSED_LINE, menu=False, on_demand=True)
    rig.c.resume()
    assert rig.overlay.strip is None  # pausing or resuming closes it
    rig.c.handle_snapshot(snap(gated="password", field_id="f2"))
    assert rig.overlay.status == StatusView(StatusState.MUTED, message=PRIVATE_LINE)
    rig.c.toggle_strip()
    assert rig.overlay.strip.note == PRIVATE_LINE and not rig.overlay.strip.menu
    rig.c.handle_snapshot(snap(gated="password", field_id="f2", seq=2))
    assert rig.overlay.strip is not None  # the next snapshot keeps it open


def test_no_ai_is_muted_but_local_fixes_still_count(rig):
    ai = {"ai": "none"}
    rig.c.extra_chip_inputs.append(lambda s: dict(ai))
    _settle(rig)
    assert rig.overlay.status == StatusView(StatusState.READY, 1)
    _settle(rig, snap(text="The quick brown fox jumps over it.", seq=2))
    assert rig.overlay.status == StatusView(StatusState.MUTED, message=NO_AI_LINE)
    rig.c.toggle_strip()
    assert rig.overlay.strip.note == NO_AI_LINE and rig.overlay.strip.menu


def test_click_toggles_the_strip_in_on_click_mode(rig):
    _settle(rig)
    assert rig.overlay.strip is None
    rig.c.toggle_strip()
    strip = rig.overlay.strip
    assert strip.can_use and strip.text.startswith("The") and strip.on_demand
    rig.c.toggle_strip()
    assert rig.overlay.strip is None
    rig.c.toggle_strip()
    assert rig.overlay.strip is not None


def test_opened_strip_with_nothing_says_so(rig):
    _settle(rig, snap(text="The quick brown fox jumps over it."))
    rig.c.toggle_strip()
    assert rig.overlay.strip == StripView(note=NOTHING_LINE, on_demand=True)
    rig.c.handle_snapshot(snap(text="The quick brown fox jumps over it!", seq=2))
    assert rig.overlay.strip.note == NOTHING_LINE  # typing: held until checked


def test_opened_strip_while_working_says_checking_then_updates_in_place(rig):
    rig.c.handle_snapshot(snap())
    rig.c.toggle_strip()
    assert rig.overlay.strip.note == CHECKING_LINE
    rig.clock.advance(400)
    rig.deliver()
    assert rig.overlay.strip.can_use and rig.overlay.strip.on_demand
    first = rig.overlay.strip
    rig.c.handle_snapshot(snap(text=TEH + " and teh dog", seq=2))
    assert rig.overlay.strip == first  # typing: kept, not flickering
    rig.clock.advance(400)
    rig.deliver()
    assert rig.overlay.strip.n_fixes == 2  # updated in place, still open


def test_strip_closes_on_focus_change_apply_and_escape(rig):
    _settle(rig)
    rig.c.toggle_strip()
    _settle(rig, snap(field_id="f2", seq=2))
    assert rig.overlay.strip is None  # another field
    rig.c.toggle_strip()
    rig.c.use_strip()
    assert rig.capture.applies and rig.overlay.strip is None  # an apply
    rig.c.handle_apply_result(ApplyResult(rig.capture.applies[-1].apply_id, "verified", ""))
    _settle(rig, snap(text=TEH + " again", field_id="f2", seq=3))  # the new text
    assert rig.overlay.strip is None  # and it stays closed
    rig.c.toggle_strip()
    rig.c.close_panel(snooze=True)  # Escape
    assert rig.overlay.strip is None
    assert rig.overlay.status.state is StatusState.READY  # the icon stays: no snooze
    rig.c.toggle_strip()
    assert rig.overlay.strip is not None


def test_more_closes_only_the_drawer_and_the_panel_hotkey_opens_both(rig):
    _settle(rig)
    rig.c.open_panel()  # Super+Alt+Space
    assert rig.overlay.strip.expanded and rig.overlay.panel is not None
    rig.c.toggle_panel()  # "⋯"
    assert rig.overlay.panel is None and rig.overlay.strip is not None
    rig.c.toggle_panel()
    assert rig.overlay.panel is not None
    rig.c.close_panel()  # the drawer's Close
    assert rig.overlay.panel is None and rig.overlay.strip is None


def test_auto_mode_shows_the_strip_by_itself_and_the_icon_too(rig):
    rig.cfg = AUTO
    _settle(rig)
    assert rig.overlay.strip.can_use and not rig.overlay.strip.on_demand
    assert rig.overlay.status == StatusView(StatusState.READY, 1)
    rig.c.toggle_strip()  # the icon hides it for this field...
    assert rig.overlay.strip is None and rig.overlay.status.state is StatusState.READY
    rig.c.toggle_strip()  # ...and brings it back
    assert rig.overlay.strip.on_demand


def test_mode_switch_applies_live(rig):
    _settle(rig)
    assert rig.overlay.strip is None
    rig.cfg = AUTO
    rig.c.refresh()
    assert rig.overlay.strip is not None
    rig.cfg = Config()
    rig.c.refresh()
    assert rig.overlay.strip is None


def test_anchor_says_whether_text_follows_the_caret(rig):
    s = snap(text="Teh fox\nsecond")
    rig.c.handle_snapshot(type(s)(**{**s.__dict__, "caret": 2}))
    assert rig.overlay.status_anchor.tail is True
    rig.c.handle_snapshot(type(s)(**{**s.__dict__, "caret": 7, "seq": 2}))
    rig.clock.advance(400)  # a caret move is drawn once it settles
    assert rig.overlay.status_anchor.tail is False


# ---------------------------------------------------------------- Escape and the strip

FIX = StripView((Segment("The", True),), 1)
AT = Anchor(caret=Rect(10, 10, 1, 18), field=Rect(0, 0, 400, 40), pid=7)


def test_escape_is_grabbed_only_while_an_opened_strip_or_the_drawer_is_up():
    overlay, keys, esc_calls = FakeOverlay(), FakeHotkeys(), []
    esc = PanelEscape(overlay, keys, lambda: esc_calls.append(1), on_use=lambda: None)
    esc.show_strip(FIX, AT)  # shown by itself ("auto"): Escape stays the app's
    assert "escape" not in keys.grabbed()
    esc.show_strip(StripView(FIX.segments, 1, on_demand=True), AT)
    esc.show_strip(StripView(FIX.segments, 1, on_demand=True), AT)
    assert keys.grabbed()["escape"] == "Escape" and keys.log.count(("grab", "escape")) == 1
    keys.press("escape")
    assert esc_calls == [1]
    from glance.ui.panel_model import PanelView

    esc.show_panel(PanelView(), 0, 0, anchor=AT)
    esc.hide_strip()
    assert "escape" in keys.grabbed()  # the drawer still wants it
    esc.hide_panel()
    assert "escape" not in keys.grabbed() and esc.escape_grabbed is False


# ---------------------------------------------------------------- the setting


def test_suggestions_default_and_auto(tmp_path):
    assert Config().ui.suggestions == "on_click"
    p = tmp_path / "config.toml"
    p.write_text('[ui]\nsuggestions = "auto"\n')
    assert load_config(p).ui.suggestions == "auto"


def test_a_bad_suggestions_value_is_the_default_with_a_warning(tmp_path, capsys):
    p = tmp_path / "config.toml"
    p.write_text('[ui]\nsuggestions = "secret-typo"\nuse_key = "Super+Return"\n')
    cfg = load_config(p)
    assert cfg.ui.suggestions == "on_click" and cfg.ui.use_key == "Super+Return"
    err = capsys.readouterr().err
    assert "config_value_ignored key=ui.suggestions" in err
    assert "secret-typo" not in err


def test_tray_toggle_writes_the_config_keeping_its_layout_and_reloads(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("# my settings\n[ui]\nuse_key = \"Alt+Return\"  # keep\n")
    reloaded = []
    set_suggestions(update_config, p, True, lambda: reloaded.append(1))
    text = p.read_text()
    assert "# my settings" in text and "# keep" in text
    assert load_config(p).ui.suggestions == "auto" and reloaded == [1]
    set_suggestions(update_config, p, False, lambda: reloaded.append(2))
    assert load_config(p).ui.suggestions == "on_click" and reloaded == [1, 2]


def test_tray_toggle_on_a_broken_config_changes_nothing(tmp_path, capsys):
    p = tmp_path / "config.toml"
    p.write_text("this is [not toml\n")
    reloaded = []
    set_suggestions(update_config, p, True, lambda: reloaded.append(1))
    assert p.read_text() == "this is [not toml\n" and reloaded == []
    assert "config_write_failed" in capsys.readouterr().err


def test_ui_cfg_field_documented():
    assert UiCfg().suggestions == "on_click"
