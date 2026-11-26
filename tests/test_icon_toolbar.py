"""The status icon's toolbar (`ui.icon_toolbar`): what it offers per state, where it goes,
and where each click leads: Check -> the check flow; a tone -> its rewrite previewed in the
strip (never applied before Use); "Rewrite…" / "⋯" -> the Glance Assistant panel."""
from __future__ import annotations

import pytest

from glance.daemon.panel_actions import PanelRouter
from glance.protocol import Rect
from glance.ui.icon_toolbar import (
    ACTION,
    GAP,
    TYPING_QUIET_MS,
    icon_toolbar,
    may_open,
    place_toolbar,
    tone_key,
    toolbar_piece,
)
from glance.ui.status_model import (
    AI_LIMIT_LINE,
    NO_AI_LINE,
    PAUSED_LINE,
    PRIVATE_LINE,
    StatusState,
    StatusView,
)
from tests import test_check_shortcut as cs
from tests import test_controller as ct
from tests import test_daemon_ai_wiring as aw


def view(state, message=None):
    return StatusView(state, message=message)


# ---------------------------------------------------------------- the model


def test_items_in_the_normal_state():
    tb = icon_toolbar(view(StatusState.IDLE), "Super+Alt+G")
    assert [a.label for a in tb.actions] == ["Check", "Friendlier", "Direct", "Shorter",
                                            "Professional", "Rewrite…", "⋯"]
    assert tb.item_keys() == ["check", "tone:friendlier", "tone:more_direct", "tone:shorter",
                         "tone:professional", "rewrite", "more"]
    assert tb.note is None and tb.hint == "Super+Alt+G"
    assert tb.actions[0].tip == "Check spelling & grammar (Super+Alt+G)"
    # line icons, never emoji: every item but ⋯ has a glyph from look.GLYPHS
    from glance.ui.look import GLYPHS

    assert all(a.glyph in GLYPHS for a in tb.actions[:-1]) and tb.actions[-1].glyph is None
    assert icon_toolbar(view(StatusState.READY), "⌃⌥G").hint == "⌃⌥G"


def test_tones_follow_the_audience_profile():
    tb = icon_toolbar(view(StatusState.IDLE), tones=("sharpen_prompt", "add_structure",
                                                     "shorter", "custom", "bogus"))
    assert tb.item_keys()[1:4] == ["tone:sharpen_prompt", "tone:add_structure", "tone:shorter"]
    assert [a.label for a in tb.actions[1:4]] == ["Sharpen", "Structure", "Shorter"]
    five = icon_toolbar(view(StatusState.IDLE), tones=("professional", "friendlier", "shorter",
                                                       "more_direct", "add_structure"))
    assert len([k for k in five.item_keys() if tone_key(k)]) == 4
    assert five.item_keys()[1] == "tone:professional"


@pytest.mark.parametrize("message,ai,keys,note", [
    (PAUSED_LINE, "ok", ["resume"], "Glance is paused."),
    (PRIVATE_LINE, "ok", [], PRIVATE_LINE),
    (NO_AI_LINE, "none", ["setup"], "No AI connected: spelling and grammar still work."),
    (AI_LIMIT_LINE, "limit", ["check"], "AI paused (limit reached)."),
])
def test_muted_states_are_one_line_with_their_single_action(message, ai, keys, note):
    tb = icon_toolbar(view(StatusState.MUTED, message), "Super+Alt+G", ai)
    assert tb.item_keys() == keys and tb.note == note
    assert "rewrite" not in tb.item_keys() and "more" not in tb.item_keys()


def test_no_ai_even_before_the_icon_says_so_and_hidden_has_none():
    assert icon_toolbar(view(StatusState.IDLE), ai="none").item_keys() == ["setup"]
    assert icon_toolbar(view(StatusState.HIDDEN)) is None


def test_piece_for_the_shell():
    piece = toolbar_piece(icon_toolbar(view(StatusState.IDLE), "Super+Alt+G"))
    assert piece["action"] == ACTION == "icon" and piece["hint"] == "Super+Alt+G"
    assert piece["actions"][1] == {"key": "tone:friendlier", "label": "Friendlier",
                                   "glyph": "heart", "tip": piece["actions"][1]["tip"]}


def test_tone_key():
    assert tone_key("tone:shorter") == "shorter"
    assert tone_key("tone:") is None and tone_key("rewrite") is None


def test_typing_suppresses_hover_open():
    assert may_open(10.0, None) and may_open(10.0, 10.0 - TYPING_QUIET_MS / 1000)
    assert not may_open(10.0, 9.9)


# ---------------------------------------------------------------- placement

MON = [Rect(0, 0, 1920, 1080)]


def test_placed_below_the_caret_line_right_aligned_with_the_icon():
    icon, line = Rect(600, 200, 28, 28), Rect(300, 204, 2, 20)
    x, y = place_toolbar(icon, line, (560, 36), MON)
    assert (x, y) == (628 - 560, 228 + GAP)
    assert y >= line.y + line.h  # never over the caret line


def test_goes_above_without_room_below_and_stays_on_the_monitor():
    icon, line = Rect(1900, 1050, 28, 28), Rect(1800, 1054, 2, 20)
    x, y = place_toolbar(icon, line, (560, 36), MON)
    assert y + 36 <= line.y and x + 560 <= 1920
    x, _y = place_toolbar(Rect(10, 100, 28, 28), None, (560, 36), MON)
    assert x == 0


# ---------------------------------------------------------------- the controller


def test_controller_offers_none_while_typing_unless_right_clicked(tmp_path):
    rig = cs.Rig(tmp_path)
    rig.c.handle_snapshot(ct.snap())
    rig.clock.advance(1000)
    rig.c.handle_snapshot(ct.snap(ct.TEH + " o", seq=2))
    assert rig.c.icon_toolbar("Super+Alt+G") is None  # a key just now
    assert rig.c.icon_toolbar("Super+Alt+G", typing_ok=True).item_keys()[0] == "check"
    rig.clock.advance(TYPING_QUIET_MS)
    assert rig.c.icon_toolbar("Super+Alt+G").item_keys()[0] == "check"


def test_controller_toolbar_uses_the_profiles_tones(tmp_path):
    d = aw.Daemon(tmp_path)
    d.focus()
    tb = d.c.icon_toolbar("Super+Alt+G")
    assert [tone_key(k) for k in tb.item_keys() if tone_key(k)] == list(d.ai.icon_tones())


# ---------------------------------------------------------------- routing


def test_check_runs_the_check_flow(tmp_path):
    rig = cs.Rig(tmp_path)
    router = PanelRouter(rig.c)
    rig.c.handle_snapshot(ct.snap())
    router.action(ACTION, {"key": "check"})
    rig.clock.advance(0)
    assert rig.overlay.strip is not None and rig.overlay.strip.n_fixes > 0
    assert rig.overlay.strip.label is None and rig.overlay.panel is None


def test_resume_setup_and_malformed(tmp_path):
    rig = cs.Rig(tmp_path)
    opened = []
    router = PanelRouter(rig.c, on_setup=lambda: opened.append(1))
    rig.c.handle_snapshot(ct.snap())
    rig.c.toggle_pause()
    router.action(ACTION, {"key": "resume"})
    assert not rig.c.status()["paused"]
    router.action(ACTION, {"key": "setup"})
    assert opened == [1]
    router.action(ACTION, {})  # malformed: nothing raised
    router.action(ACTION, {"key": "bogus"})


def daemon(tmp_path):
    d = aw.Daemon(tmp_path)
    d.focus()
    return d, PanelRouter(d.c, d.ai, d.overrides.set, on_setup=lambda: None)


def test_tone_previews_in_the_strip_and_applies_only_on_use(tmp_path):
    d, router = daemon(tmp_path)
    before = len(d.rewrites())
    router.action(ACTION, {"key": "tone:friendlier"})
    assert [c.tone for c in d.rewrites()[before:]] == ["friendlier"]  # one AI call
    strip = d.overlay.strip
    assert strip is not None and strip.label == "Friendlier" and strip.on_demand
    assert strip.text == "Thanks so much, will do!"
    assert any(s.changed for s in strip.segments) and strip.can_use
    assert d.overlay.panel is None  # the big panel stays shut
    assert d.capture.applies == []  # never applied without Use
    # the same tone again: the rewrite made for this text, no second call
    router.action(ACTION, {"key": "tone:friendlier"})
    assert len(d.rewrites()) == before + 1
    d.c.use_strip()  # Use (Alt+Enter)
    assert len(d.capture.applies) == 1
    assert d.capture.applies[0].edit.replacement == "Thanks so much, will do!"


def test_esc_closes_the_preview_and_nothing_is_applied(tmp_path):
    d, router = daemon(tmp_path)
    router.action(ACTION, {"key": "tone:shorter"})
    assert d.overlay.strip is not None and d.overlay.strip.label == "Shorter"
    router.escape()
    assert d.overlay.strip is None and not d.c.preview_open
    assert d.capture.applies == []


def test_typing_drops_the_preview(tmp_path):
    d, router = daemon(tmp_path)
    router.action(ACTION, {"key": "tone:shorter"})
    assert d.c.preview_open
    d.c.handle_snapshot(aw.snap(aw.TEXT + " more"))
    assert not d.c.preview_open
    assert d.overlay.strip is None or d.overlay.strip.label is None


def test_more_opens_the_panel_and_rewrite_its_rewrite_as_input(tmp_path):
    d, router = daemon(tmp_path)
    router.action(ACTION, {"key": "more"})
    assert d.overlay.panel is not None and d.overlay.asked == []
    d.c.close_panel()
    assert d.overlay.panel is None
    router.action(ACTION, {"key": "rewrite"})
    assert d.overlay.panel is not None
    assert [prompt for prompt, _cb in d.overlay.asked] == ["Rewrite as…"]
    assert d.capture.applies == []


def test_a_tone_without_ai_wiring_does_nothing(tmp_path):
    rig = cs.Rig(tmp_path)
    router = PanelRouter(rig.c)
    rig.c.handle_snapshot(ct.snap())
    router.action(ACTION, {"key": "tone:shorter"})
    assert rig.overlay.strip is None and rig.overlay.panel is None


# ---------------------------------------------------------------- the shell's scene


def test_shell_overlay_sends_the_toolbar_in_the_scene():
    from glance.ui.overlay_shell import ShellOverlay
    from tests import test_shell_adapters as sa

    bus = sa.bus_with_monitors()
    posted, actions, asked = [], [], []

    def toolbar(on_purpose=False):
        asked.append(on_purpose)
        return icon_toolbar(view(StatusState.IDLE), "Super+Alt+G")

    ov = ShellOverlay(bus, posted.append, lambda ms, fn: None, lambda: sa.LIGHT,
                      lambda: None, lambda action, args: actions.append((action, args)),
                      icon_toolbar=toolbar)

    def run():
        while posted:
            posted.pop(0)()

    ov.on_window({"reason": "focus", "focus": sa.WINDOW, "monitors": [[0, 0, 1280, 800]]})
    ov.show_status(view(StatusState.IDLE), sa.ANCHOR)
    run()
    assert "toolbar" not in sa.last_scene(bus)["status"]
    ov.on_clicked({"target": "hover", "args": {}})
    run()
    status = sa.last_scene(bus)["status"]
    bar = status["toolbar"]
    assert [a["key"] for a in bar["actions"]][:2] == ["check", "tone:friendlier"]
    assert bar["hint"] == "Super+Alt+G" and bar["action"] == "icon"
    line = sa.ANCHOR.caret
    assert bar["y"] >= line.y + line.h  # under the caret line
    assert ov.toolbar_items()[0] == "Check"
    ov.on_clicked({"target": "action", "args": {"action": "icon", "args": {"key": "check"}}})
    run()
    assert actions == [("icon", {"key": "check"})]
    assert "toolbar" not in sa.last_scene(bus)["status"]
    # a right-click: at once and on purpose; a second one closes it
    ov.on_clicked({"target": "rightclick", "args": {}})
    run()
    assert "toolbar" in sa.last_scene(bus)["status"] and asked[-1] is True
    ov.on_clicked({"target": "rightclick", "args": {}})
    run()
    assert "toolbar" not in sa.last_scene(bus)["status"]
    # the extension's real size places it again
    ov.on_clicked({"target": "hover", "args": {}})
    ov.on_sized({"toolbar_w": 500, "toolbar_h": 36})
    run()
    assert ov._placed["toolbar"][2:] == (500, 36)
    ov.on_clicked({"target": "hover_end", "args": {}})
    run()
    assert "toolbar" not in sa.last_scene(bus)["status"]
