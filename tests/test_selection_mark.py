"""Selection actions, detection and the mark: settled events show the mark only through the
privacy gate, never read the selection, and hide on input, focus, idle and typing."""
from __future__ import annotations

from dataclasses import replace

import pytest

from glance.config import Config, SelectionCfg
from glance.features import selection_actions as sa
from glance.features.select_anything import SelectAnything
from glance.features.selection_actions import SelectionActions
from glance.privacy.gate import PrivacyGate
from glance.ui import selection_bar as sb
from tests.fakes import FakeSelection, win
from tests.test_ai_analyze import Rig, snap

SEL = "the quarterly numbers look fine to me"


class Watch:
    def __init__(self):
        self.started = 0
        self.stopped = 0
        self.armed: list[bool] = []
        self.cb = None

    def start(self, settled, cleared, on_input):
        self.started += 1
        self.cb = (settled, cleared, on_input)

    def stop(self):
        self.stopped += 1

    def arm(self, on):
        self.armed.append(on)


class Bar:
    def __init__(self, tmp_path, cfg=None):
        self.rig = r = Rig(tmp_path, patterns={"teh": ("Typo", ("the",))})
        if cfg is not None:
            r.cfg = cfg
        r.backend.deltas = ("An explanation.",)
        self.sel = FakeSelection()
        self.sa = SelectAnything(
            r.c, self.sel, lambda: PrivacyGate(r.cfg), lambda: r.cfg, r.harper, r.c._filter,
            r.sup, r.ai._resolver, r.overlay, r.c._capture, r.notifier, r.bg)
        r.c.add_listener(self.sa)
        self.watch = Watch()
        self.bar = SelectionActions(
            controller=r.c, select_anything=self.sa, selection=self.sel, watch=self.watch,
            cfg=lambda: r.cfg, overlay=r.overlay, clock=r.clock, notifier=r.notifier,
            run_bg=r.bg, supervisor=r.sup, resolver=r.ai._resolver, grammar=r.harper,
            issue_filter=r.c._filter, remember=self.remember, hint=lambda: "Super+Alt+A")
        r.c.add_listener(self.bar)
        self.bar.sync()
        self.remembered: list[tuple[str, object]] = []

    def remember(self, key, value):
        self.remembered.append((key, value))
        sel = replace(self.rig.cfg.selection, **{key.split(".")[1]: value})
        self.rig.cfg = replace(self.rig.cfg, selection=sel)

    @property
    def overlay(self):
        return self.rig.overlay

    def select(self, text=SEL, wm_class="Slack", xid=9, title="general - Slack", pid=100):
        w = win(wm_class, xid=xid, title=title, pid=pid)
        self.sel.set(text, w)
        return w

    def settle(self, x=400, y=300, **kw):
        self.bar.on_settled(x, y, **kw)


@pytest.fixture
def b(tmp_path):
    return Bar(tmp_path)


def test_on_by_default_and_the_switch_starts_and_stops_the_listener(b):
    assert b.watch.started == 1 and b.bar.watching
    b.select()
    b.settle()
    assert b.overlay.selmark is not None
    b.rig.cfg = replace(b.rig.cfg, selection=replace(b.rig.cfg.selection, enabled=False))
    b.bar.sync()
    assert b.watch.stopped == 1 and not b.bar.watching and b.overlay.selmark is None
    b.bar.on_settled(1, 1)
    assert b.overlay.selmark is None
    b.rig.cfg = replace(b.rig.cfg, selection=replace(b.rig.cfg.selection, enabled=True))
    b.bar.sync()
    assert b.watch.started == 2


def test_settled_shows_the_mark_above_right_and_reads_nothing(b):
    b.select()
    b.settle(400, 300)
    assert b.overlay.selmark.state == "mark"
    assert b.overlay.selmark_xy == (406, 300 - 8 - sb.MARK_BOX[1])
    assert b.sel.reads == [] and b.rig.backend.calls == [] and b.rig.harper.calls == []
    assert b.watch.armed == [True]


@pytest.mark.parametrize("wm_class,title", [
    ("keepassxc", "vault"),                                # denylisted app
    ("Slack", "Inbox - Google Chrome (Incognito)"),        # private window
    ("com.mitchellh.ghostty", "~"),                        # a terminal: off by default
    ("firefox", "Sign in - mybank.com"),                   # a sensitive page by its title
])
def test_the_gate_decides_before_the_mark_shows(b, wm_class, title):
    b.select(wm_class=wm_class, title=title)
    b.settle()
    assert b.overlay.selmark is None and b.sel.reads == []
    assert b.rig.notifier.calls == []  # quiet: nobody asked for anything


def test_terminals_opt_in(tmp_path):
    b = Bar(tmp_path, Config(selection=SelectionCfg(terminals=True)))
    b.select(wm_class="com.mitchellh.ghostty", title="~")
    b.settle()
    assert b.overlay.selmark is not None


def test_password_field_of_that_window_blocks_the_mark(b):
    b.rig.c.handle_snapshot(snap("", gated="password"))  # pid 100
    b.select(pid=100)
    b.settle()
    assert b.overlay.selmark is None


def test_selection_of_another_window_shows_nothing(b):
    b.sel.set(SEL, win("keepassxc", xid=7), win("Slack", xid=9))
    b.settle()
    assert b.overlay.selmark is None


def test_paused_shows_nothing(b):
    b.rig.c.toggle_pause()
    b.select()
    b.settle()
    assert b.overlay.selmark is None


def test_no_mark_while_typing_in_the_field(b):
    b.rig.c.handle_snapshot(snap("hello"))
    b.rig.c.handle_snapshot(snap("hello there"))  # the text changed: typing
    b.select()
    b.settle()
    assert b.overlay.selmark is None
    b.rig.clock.advance(sb.TYPING_QUIET_MS + 10)
    b.settle()
    assert b.overlay.selmark is not None
    b.rig.c.handle_snapshot(snap("hello there!"))  # typing again hides it
    assert b.overlay.selmark is None


def test_a_known_short_length_shows_nothing(b):
    b.select()
    b.settle(length=1)
    assert b.overlay.selmark is None
    b.settle(length=12)
    assert b.overlay.selmark is not None


@pytest.mark.parametrize("kind", ["key", "scroll", "focus"])
def test_input_hides_the_mark(b, kind):
    b.select()
    b.settle()
    b.bar.on_input(kind, 0, 0)
    assert b.overlay.selmark is None and b.watch.armed == [True, False]


def test_a_click_on_the_mark_does_not_hide_it_a_click_elsewhere_does(b):
    b.select()
    b.settle()
    b.overlay.hit = True
    b.bar.on_input("click", 410, 280)
    assert b.overlay.selmark is not None
    b.overlay.hit = False
    b.bar.on_input("click", 10, 10)
    assert b.overlay.selmark is None


def test_cleared_and_idle_hide_it(b):
    b.select()
    b.settle()
    b.bar.on_cleared()
    assert b.overlay.selmark is None
    b.settle()
    b.rig.clock.advance(sb.IDLE_MS - 10)
    assert b.overlay.selmark is not None
    b.rig.clock.advance(20)
    assert b.overlay.selmark is None


def test_hover_keeps_it_and_resting_opens_the_bar(b):
    b.select()
    b.settle()
    b.bar.hover(True)
    b.rig.clock.advance(sb.OPEN_MS)
    assert b.overlay.selmark.state == "bar"
    b.rig.clock.advance(sb.IDLE_MS * 2)
    assert b.overlay.selmark is not None  # nobody hides a bar under the pointer
    b.bar.hover(False)
    b.rig.clock.advance(sb.IDLE_MS)
    assert b.overlay.selmark is None
    assert b.sel.reads == []  # still nothing read


def test_click_opens_the_bar_with_its_actions(b):
    b.select()
    b.settle()
    b.bar.open_bar()
    v = b.overlay.selmark
    assert [a.key for a in v.actions] == ["explain", "summarize", "translate", "fix",
                                          "rewrite", "reply", "copy", "more"]
    assert v.translate_to == "English" and v.hint == "Super+Alt+A"
    b.bar.toggle_more()
    assert b.overlay.selmark.more[0].key == "translate_to"
    assert all(a.key.startswith("tone:") for a in b.overlay.selmark.more[1:])


def test_window_change_hides(b):
    b.select()
    b.settle()
    b.bar.on_window()
    assert b.overlay.selmark is None


# ------------------------------------------------------------------ the X11 watcher


class _Q:
    root_x, root_y = 120, 80


class _Root:
    def query_pointer(self):
        return _Q()


def _xwatch():
    from glance.ui.selection_watch_xlib import XlibSelectionWatch
    from tests.fakes import FakeClock

    clock = FakeClock()
    w = XlibSelectionWatch(clock.call_later, clock.now)
    w._d, w._root, w._xi = object(), _Root(), 131
    out = []
    w._on_settled = lambda x, y: out.append(("settled", x, y))
    w._on_cleared = lambda: out.append(("cleared",))
    w._on_input = lambda kind, x, y: out.append(("input", kind))
    return w, clock, out


def _raw(detail):
    import struct
    return struct.pack("=HII", 2, 0, detail)


def test_xwatch_mouse_selection_settles_after_release():
    from Xlib.ext import xinput

    w, clock, out = _xwatch()
    w._raw(xinput.RawButtonPress, _raw(1))
    w._owner_changed(0x400001)
    clock.advance(1000)
    assert out == []  # the button is still held
    w._raw(xinput.RawButtonRelease, _raw(1))
    clock.advance(sb.SETTLE_MS - 100)
    assert out == []
    clock.advance(200)
    assert out == [("settled", 120, 80)]


def test_xwatch_keyboard_selection_and_owner_gone_clear():
    w, clock, out = _xwatch()
    w._owner_changed(0x400001)  # no button: Shift+arrows or Ctrl+A
    w._owner_changed(0)
    clock.advance(1000)
    assert out == [("cleared",), ("cleared",)]


def test_xwatch_reports_keys_and_scroll_only_when_armed():
    from Xlib.ext import xinput

    w, _clock, out = _xwatch()
    w._raw(xinput.RawKeyPress, _raw(38))
    w._raw(xinput.RawButtonPress, _raw(4))
    assert out == []
    w._armed = True
    w._raw(xinput.RawKeyPress, _raw(38))
    w._raw(xinput.RawButtonPress, _raw(5))
    w._raw(xinput.RawButtonPress, _raw(1))
    assert out == [("input", "key"), ("input", "scroll"), ("input", "click")]


# ------------------------------------------------------------------ the actions


def _bar(b, text=SEL, **kw):
    b.select(text, **kw)
    b.settle()
    b.bar.open_bar()


def test_explain_reads_only_on_click_and_shows_the_answer(b):
    _bar(b)
    assert b.sel.reads == []
    b.bar.act("explain")
    assert len(b.sel.reads) == 1 and b.overlay.selmark is None
    (call,) = [c for c in b.rig.backend.calls if c.kind == "rewrite"]
    assert call.tone == "explain" and call.text == SEL and call.custom is None
    v = b.overlay.panel
    assert v.mode == "result" and v.result.title == "Explain"
    assert v.result.text == "An explanation." and v.result.state == "ready"
    assert not v.result.can_replace  # an explanation never replaces the selection
    b.bar.copy_result()
    assert b.overlay.clipboard == ["An explanation."] and b.overlay.panel is None


def test_secrets_are_shielded_before_the_ai_sees_them(b):
    secret = "sk-ant-api03-AbCdEf0123456789xyzQRSTUV"
    _bar(b, f"why does {secret} fail here")
    b.bar.act("summarize")
    (call,) = [c for c in b.rig.backend.calls if c.kind == "rewrite"]
    assert secret not in call.text and "[[GLANCE_" in call.text


def test_translate_names_the_target_language(b):
    _bar(b)
    assert next(a for a in b.overlay.selmark.actions if a.key == "translate").tip == \
        "Translate to English"
    b.bar.act("translate")
    (call,) = [c for c in b.rig.backend.calls if c.kind == "rewrite"]
    assert call.tone == "translate" and call.custom == "Target language: English"
    assert b.overlay.panel.result.title == "Translate to English"


def test_translate_to_asks_remembers_and_translates(b):
    _bar(b)
    b.bar.toggle_more()
    b.bar.act("translate_to")
    prompt, done = b.overlay.asked[-1]
    assert prompt == "Translate to which language?"
    done("  Bangla ", True)
    assert b.remembered == [("selection.translate_to", "Bangla")]
    (call,) = [c for c in b.rig.backend.calls if c.kind == "rewrite"]
    assert call.custom == "Target language: Bangla"
    assert b.overlay.panel.result.title == "Translate to Bangla"
    b.bar.close()
    _bar(b)
    assert b.overlay.selmark.translate_to == "Bangla"


def test_fix_grammar_is_ai_first_and_replaces_through_verified_write_back(b):
    text = "teh plan is good"
    b.rig.c.handle_snapshot(snap(f"I think {text}, ok?"))  # the field the selection is in
    b.rig.clock.advance(sb.TYPING_QUIET_MS + 10)
    b.rig.backend.deltas = ("the plan is good",)
    _bar(b, text)
    b.bar.act("fix")
    b.rig.clock.advance(0)
    (call,) = [c for c in b.rig.backend.calls if c.kind == "rewrite"]
    assert call.tone == "fix_grammar"
    v = b.overlay.panel.result
    assert v.text == "the plan is good" and v.state == "ready" and v.can_replace
    b.bar.replace()
    (req,) = b.rig.c._capture.applies
    assert (req.edit.start, req.edit.end, req.edit.replacement) == (8, 8 + len(text),
                                                                   "the plan is good")
    assert b.overlay.panel is None


def test_fix_grammar_falls_back_to_the_local_checker_when_the_ai_fails(b):
    from glance.backends.base import BackendError

    text = "teh plan is good"
    b.rig.c.handle_snapshot(snap(f"I think {text}, ok?"))
    b.rig.clock.advance(sb.TYPING_QUIET_MS + 10)
    b.rig.backend.script(BackendError("unavailable"), BackendError("unavailable"), BackendError("unavailable"))
    _bar(b, text)
    b.bar.act("fix")
    b.rig.clock.advance(0)
    v = b.overlay.panel.result
    assert v.text == "the plan is good" and v.state == "ready" and v.can_replace
    assert v.note == sa.LOCAL_ONLY

def test_replace_is_never_offered_without_a_verified_field(b):
    _bar(b)
    b.rig.backend.deltas = ("Los números trimestrales me parecen bien",)
    b.bar.act("translate")
    v = b.overlay.panel.result
    assert v.state == "ready" and not v.can_replace
    b.bar.replace()
    assert b.rig.c._capture.applies == [] and b.rig.c._capture.pastes == []


def test_reply_asks_what_to_say_and_drafts(b):
    _bar(b, "Can you send the report by Friday?")
    b.rig.backend.deltas = ("Sure, I will send it Friday morning.",)
    b.bar.act("reply")
    b.overlay.asked[-1][1]("", True)  # nothing in particular
    (call,) = [c for c in b.rig.backend.calls if c.kind == "rewrite"]
    assert call.tone == "reply" and call.custom is None
    assert b.overlay.panel.result.title == "Reply"


def test_copy_and_rewrite(b):
    _bar(b)
    b.bar.act("copy")
    assert b.overlay.clipboard == [SEL] and b.overlay.panel is None
    _bar(b)
    b.bar.act("rewrite")
    assert b.sa.is_open and b.overlay.panel.mode == "selection"


def test_a_tone_from_more_rewrites(b):
    _bar(b)
    b.bar.toggle_more()
    tone = b.overlay.selmark.more[1].key
    b.rig.backend.deltas = ("Numbers look fine.",)
    b.bar.act(tone)
    (call,) = [c for c in b.rig.backend.calls if c.kind == "rewrite"]
    assert call.tone == tone.split(":")[1]
    assert b.overlay.panel.result.title.startswith("Rewrite: ")


def test_follow_up_asks_about_the_same_selection(b):
    _bar(b)
    b.bar.act("explain")
    b.bar.follow_up()
    b.rig.backend.deltas = ("Yes.",)
    b.overlay.asked[-1][1]("is that good news?", True)
    calls = [c for c in b.rig.backend.calls if c.kind == "rewrite"]
    assert calls[-1].tone == "ask" and calls[-1].custom == "is that good news?"
    assert calls[-1].text == SEL
    assert b.overlay.panel.result.title == "Answer" and b.overlay.panel.result.text == "Yes."


def test_no_ai_says_how_to_connect_one(tmp_path):
    from glance.config import BackendCfg

    b = Bar(tmp_path, Config(backend=BackendCfg(choice=None)))
    b.rig.backend.health_value = None
    _bar(b)
    b.bar.act("explain")
    r = b.overlay.panel.result
    assert r.state in ("failed", "ready")


def test_the_bar_gates_again_on_click(b):
    _bar(b)
    b.sel.set(SEL, win("keepassxc", xid=9))  # the window became something private
    b.bar.act("explain")
    assert b.sel.reads == [] and b.overlay.panel is None


def test_hotkey_works_with_the_mark_switched_off(tmp_path):
    b = Bar(tmp_path, Config(selection=SelectionCfg(enabled=False)))
    assert b.watch.started == 0
    b.select()
    b.bar.hotkey()
    assert b.overlay.selmark.state == "bar" and b.sel.reads == []
    b.bar.act("explain")
    assert b.overlay.panel.mode == "result"


def test_hotkey_refusal_is_said(b):
    b.sel.set(SEL, win("keepassxc", xid=7))
    b.bar.hotkey()
    assert b.overlay.selmark is None
    assert [n[0] for n in b.rig.notifier.calls] == ["Glance does not read this window"]


# ------------------------------------------------------------------ GNOME Shell adapters


def test_shell_watch_turns_the_extension_on_and_off_and_forwards_events():
    from glance.ui.selection_shell import ShellSelectionWatch
    from tests.test_shell_adapters import FakeBus

    bus = FakeBus({"WatchSelection": {"ok": True}})
    w = ShellSelectionWatch(bus)
    seen = []
    w.on_settled({"x": 1, "y": 2})  # not started: ignored
    w.start(lambda x, y: seen.append(("s", x, y)), lambda: seen.append(("c",)),
            lambda kind, x, y: seen.append(("i", kind)))
    w.on_settled({"x": 10, "y": 20})
    w.on_settled({"x": "bad"})
    w.on_cleared({})
    w.on_window({})
    w.resync()
    w.stop()
    w.on_settled({"x": 3, "y": 4})
    assert seen == [("s", 10, 20), ("c",), ("i", "focus")]
    assert bus.calls == [("WatchSelection", {"on": True}), ("WatchSelection", {"on": True}),
                         ("WatchSelection", {"on": False})]


def test_shell_overlay_sends_the_mark_and_the_bar_in_the_scene():
    from glance.protocol import Rect
    from glance.ui.overlay_shell import ShellOverlay
    from tests.test_shell_adapters import FakeBus

    bus = FakeBus({"Monitors": [[0, 0, 1280, 800]]})
    ov = ShellOverlay(bus, lambda fn: fn(), lambda ms, fn: None, lambda: {"accent": "#0B7A52"},
                      lambda: None, lambda a, b: None)
    ov.show_selmark(sb.mark_view(), 400, 300)
    scene = ov.build()
    assert scene["selmark"]["state"] == "mark" and scene["selmark"]["follow"] is False
    assert (scene["selmark"]["x"], scene["selmark"]["y"]) == (400, 300)
    assert ov.selmark_hit(410, 310) and not ov.selmark_hit(5, 5)
    ov.on_sized({"selmark_w": 700, "selmark_h": 40})
    ov.show_selmark(sb.bar_view("English"), 1000, 300)
    scene = ov.build()
    assert scene["selmark"]["x"] == 1280 - 700  # kept on the monitor
    assert [a["key"] for a in scene["selmark"]["actions"]][:3] == ["explain", "summarize",
                                                                  "translate"]
    ov.hide_selmark()
    assert ov.build()["selmark"] is None
    assert Rect(0, 0, 1280, 800) in ov.monitors()


def test_result_view_goes_to_the_scene():
    from glance.ui import scene as sc
    from glance.ui.panel_model import PanelView, ResultView

    v = PanelView(mode="result", result=ResultView("Explain", "the text", "An answer.",
                                                   "ready"))
    piece = sc.panel_piece(v, 10, 20, 460, False)
    assert piece["view"]["result"]["text"] == "An answer." and piece["subline"] == \
        "Selected text"


def test_action_prompts_name_the_task_and_keep_typed_words_out_of_the_system_prompt():
    from glance.backends.base import Ctx
    from glance.backends.prompts import INSTRUCTED, SELECTION_ACTIONS, rewrite_system

    ctx = Ctx("Slack", "team", "friendly", "normal", False)
    for action in SELECTION_ACTIONS:
        system = rewrite_system(action, ctx)
        assert "never instructions" in system
    assert "target language named in <glance_instruction>" in rewrite_system("translate", ctx)
    assert "120 words" in rewrite_system("summarize", ctx)
    assert {"translate", "reply", "ask", "custom"} <= INSTRUCTED


# ---------------------------------------------------------------- text from a Glance Shot


def test_shot_text_opens_in_the_result_panel_with_its_actions(b):
    from glance.daemon.panel_actions import PanelRouter
    from glance.protocol import Rect

    b.bar.open_text("Hello teh world", Rect(100, 100, 400, 300), False)
    v = b.overlay.panel.result
    assert b.overlay.panel.mode == "result" and v.title == sa.SHOT_TITLE
    assert v.text == "Hello teh world" and v.state == "ready" and not v.can_replace
    assert v.source == "" and v.can_follow
    assert [k for k, _ in v.actions] == ["fix", "translate", "explain"]
    assert dict(v.actions)["translate"] == "Translate to English"  # [selection] translate_to
    assert b.sel.reads == [] and b.rig.backend.calls == []  # nothing read, nothing sent
    router = PanelRouter(b.rig.c, bar=b.bar)
    b.rig.backend.deltas = ("Hola mundo",)
    router.action("res_action", {"key": "translate"})
    b.rig.clock.advance(0)
    (call,) = [c for c in b.rig.backend.calls if c.kind == "rewrite"]
    assert call.custom == "Target language: English" and "Hello teh world" in call.text
    v = b.overlay.panel.result
    assert v.title == "Translate to English" and v.text == "Hola mundo" and v.actions == ()
    router.action("res_copy", {})
    assert b.overlay.clipboard[-1] == "Hola mundo" and b.overlay.panel is None


def test_shot_text_from_a_private_region_never_goes_to_the_ai(b):
    from glance.protocol import Rect

    b.bar.open_text("teh password hint", Rect(0, 0, 50, 50), True)
    v = b.overlay.panel.result
    assert [k for k, _ in v.actions] == ["fix"] and not v.can_follow
    assert v.note == sa.NOTES["private"]
    b.bar.result_action("explain")  # not offered: refused
    b.bar.follow_up()
    b.bar.result_action("fix")
    b.rig.clock.advance(0)
    assert b.rig.backend.calls == []
    assert b.overlay.panel.result.text == "the password hint"  # the local checker alone
