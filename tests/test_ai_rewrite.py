"""Tone chips: the one rewrite path, previews, apply, speculation and "Rewrite as…"."""
import pytest

from glance.audience.profiles import BUILTIN
from glance.audience.resolver import DEFAULT_RULES, AudienceResolver
from glance.backends.base import BackendError, Ctx
from glance.backends.budget import Budget
from glance.backends.supervisor import AiUnavailable, BackendSupervisor
from glance.config import AppCfg, BackendCfg
from glance.daemon.ai import AiCoordinator
from glance.daemon.cache import LruCache
from glance.daemon.controller import Controller
from glance.daemon.rewrite import (
    RewriteOutcome,
    finish_rewrite,
    is_rich_editor,
    partial_text,
    rewrite_text,
)
from glance.diffing import word_diff
from glance.grammar.filters import IssueFilter
from glance.grammar.store import IgnoredRules, PersonalDictionary
from glance.privacy.redact import shield
from glance.protocol import ApplyResult, Edit, FieldSnapshot, Rect, text_sha256
from glance.ui.chip_model import ChipState
from glance.ui.panel_model import suggested_box
from tests.fakes import (
    FakeBackend,
    FakeBg,
    FakeCapture,
    FakeClock,
    FakeHarper,
    FakeNotifier,
    FakeOverlay,
    FakeTray,
)
from tests.fakes import typing_config as Config  # these tests check while typing

CHANNEL = "general (Channel) - Acme - Slack"
TEXT = "send me the report now, i need it before the meeting starts."
MENTION = "ping @sam about the release notes before the meeting today"
SECRET = "sk-ant-api03-AbCdEf0123456789xyzQRSTUV"
CTX = Ctx("default", "clear, neutral", "normal", "gedit")


def snap(text=TEXT, field_id="f1", title=CHANNEL, wm_class="Slack", url=None):
    return FieldSnapshot(
        app=wm_class, wm_class=wm_class, title=title, url=url, role="text", text=text,
        caret=len(text), caret_rect=Rect(100, 200, 1, 18), prev_char_rect=None,
        field_rect=Rect(90, 190, 400, 300), window_rect=Rect(50, 50, 800, 600),
        field_id=field_id, capture_confidence=1.0, seq=1, pid=100, gated=None)


def plain(text=TEXT, **kw):
    """A snapshot of a plain-text editor (gedit): the default profile, not a rich editor."""
    return snap(text, title="notes", wm_class="gedit", **kw)


def prompt_box(text=TEXT):
    return snap(text, title="Claude - Google Chrome", wm_class="google-chrome",
                url="https://claude.ai/chat/1")


class NoOverrides:
    def get(self, key):
        return None


class Rig:
    """Controller + supervisor + coordinator. `hold` keeps rewrites open until `finish()`."""

    def __init__(self, tmp_path, choice="claude", hold=False, speculate=None, deltas=None):
        self.clock = FakeClock()
        self.cfg = Config(backend=BackendCfg(choice=choice, speculate=speculate))
        self.battery = False
        self.backend = FakeBackend(choice)
        self.backend.hold = hold
        if deltas is not None:
            self.backend.deltas = deltas
        self.spec = FakeBackend(choice, deltas=("Speculated.",))
        self.overlay = FakeOverlay()
        self.capture = FakeCapture()
        self.notifier = FakeNotifier()
        self.bg = FakeBg(self.clock, self.backend, self.spec)
        self.c = Controller(
            self.clock, lambda: self.cfg, FakeHarper({}),
            IssueFilter(PersonalDictionary(tmp_path / "dictionary.txt"),
                        IgnoredRules(tmp_path / "ignored.json"), []),
            self.overlay, self.capture, self.notifier, FakeTray(), self.bg)
        self.budget = Budget(self.clock, lambda: self.cfg.backend, tmp_path / "usage.json")
        self.sup = BackendSupervisor(
            {choice: self.backend}, lambda: self.cfg.backend, self.budget, self.clock,
            lambda: self.battery, lambda st: self.clock.post(lambda: self.ai.on_ai_status(st)),
            speculative={choice: self.spec})
        self.ai = AiCoordinator(
            self.c, self.sup, AudienceResolver(BUILTIN, DEFAULT_RULES, NoOverrides()),
            LruCache(), self.clock, self.bg, overlay=self.overlay,
            rich=lambda s: is_rich_editor(s.wm_class, self.cfg),
            writeback=lambda s: self.cfg.app(s.wm_class).writeback)
        self.ai.attach()

    def open(self, s=None):
        """Focus a field, wait out the analyze idle time and open the panel."""
        s = s or snap()
        self.c.handle_snapshot(s)
        self.clock.advance(1200)
        self.c.open_panel()
        assert self.overlay.panel is not None
        return s

    def rewrites(self, backend=None):
        return [c for c in (backend or self.backend).calls if c.kind == "rewrite"]

    @property
    def panel(self):
        return self.overlay.panel

    def use_up_budget(self):
        while self.budget.try_spend():
            pass


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


@pytest.fixture
def held(tmp_path):
    return Rig(tmp_path, hold=True)


# ---------------------------------------------------------------- the one rewrite path


def test_finish_rewrite_outcomes():
    s = shield("ping @sam now")
    assert finish_rewrite("ping @sam now", s, '"Hey [[GLANCE_E1]], ping me now"') == \
        RewriteOutcome("Hey @sam, ping me now", True, None)
    o = finish_rewrite("ping @sam now", s, "Hey, ping me now")
    assert (o.applicable, o.reason, o.text) == (False, "placeholders", "Hey, ping me now")
    assert finish_rewrite("ping @sam now", s, "ping [[GLANCE_E1]] now").reason == "no_change"
    assert finish_rewrite("a ￼ b", shield("a ￼ b"), "A [[GLANCE_E1]] b!").reason == \
        "objects"


def test_finish_rewrite_empty_runaway_and_invented_placeholders():
    s = shield("ping @sam now")
    assert finish_rewrite("ping @sam now", s, "  \n") == RewriteOutcome(None, False, "failed")
    assert finish_rewrite("ping @sam now", s, "x" * 2000) == \
        RewriteOutcome(None, False, "no_change")
    # A doubled or invented placeholder: never applicable; the copy keeps what is known.
    o = finish_rewrite("ping @sam now", s, "[[GLANCE_E1]] and [[GLANCE_E1]] and [[GLANCE_E7]]")
    assert (o.applicable, o.reason) == (False, "placeholders")
    assert o.text == "@sam and @sam and [[GLANCE_E7]]"
    # The output equals the original only once the placeholder is restored.
    plain_s = shield("all good here")
    assert finish_rewrite("all good here", plain_s, "all good here").reason == "no_change"


def test_finish_rewrite_in_a_rich_editor_never_flattens_entities():
    for text in ("ping @sam now", "party :tada: at five", "see https://example.com/a now",
                 "run `make test` first"):
        s = shield(text)
        assert s.mapping, text
        raw = "Updated: " + s.text
        assert finish_rewrite(text, s, raw).applicable is True
        o = finish_rewrite(text, s, raw, rich=True)
        assert (o.applicable, o.reason, o.text) == (False, "rich", "Updated: " + text)
    # A secret is plain text in any editor, and text without entities is always fine.
    s = shield(f"key {SECRET} ok")
    assert finish_rewrite(f"key {SECRET} ok", s, "The key [[GLANCE_S1]] is fine.", rich=True) \
        == RewriteOutcome(f"The key {SECRET} is fine.", True, None)
    assert finish_rewrite("hello there", shield("hello there"), "Hello!", rich=True).applicable


def test_is_rich_editor():
    cfg = Config(apps={"gedit": AppCfg(rich=True), "firefox": AppCfg(rich=False)})
    assert is_rich_editor("Slack", cfg) and is_rich_editor("discord", cfg)
    assert is_rich_editor("Google-chrome", cfg) and is_rich_editor("brave-browser", cfg)
    assert is_rich_editor("gedit", cfg)  # [apps.gedit] rich = true
    assert not is_rich_editor("firefox", cfg)  # the user said it is plain there
    assert not is_rich_editor("kitty", cfg) and not is_rich_editor("Gtk_app.py", Config())


def test_partial_text_hides_unfinished_placeholders():
    s = shield("ping @sam now")
    assert partial_text(s, "Hey [[GLANCE_E1]], ping") == "Hey @sam, ping"
    for tail in ("[", "[[", "[[GLA", "[[GLANCE_E1", "[[GLANCE_E1]"):
        assert partial_text(s, "Hey " + tail) == "Hey "
    assert partial_text(s, "an [aside] stays") == "an [aside] stays"


class StubSupervisor:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def rewrite(self, text, tone, ctx, custom=None, speculative=False):
        self.calls.append((text, tone, custom, speculative))
        if isinstance(self.result, BaseException):
            raise self.result
        yield from self.result


def test_rewrite_text_shields_streams_and_finishes():
    sup = StubSupervisor(["Use [[GLANCE_S1]] ", "to sign in, [[GLANCE_E1]]."])
    seen = []
    text = f"sign in with {SECRET} @sam"
    out = rewrite_text(sup, text, "shorter", CTX, on_delta=seen.append)
    assert out == RewriteOutcome(f"Use {SECRET} to sign in, @sam.", True, None)
    assert seen == ["Use [[GLANCE_S1]] ", "to sign in, [[GLANCE_E1]]."]
    assert sup.calls == [("sign in with [[GLANCE_S1]] [[GLANCE_E1]]", "shorter", None, False)]


def test_rewrite_text_never_sends_a_secret_in_the_custom_instruction():
    sup = StubSupervisor(["Arr."])
    rewrite_text(sup, "hello there friend", "custom", CTX, custom=f"sign it with {SECRET}")
    (_text, _tone, custom, _spec) = sup.calls[0]
    assert SECRET not in custom and custom.startswith("sign it with ")


def test_rewrite_text_maps_unavailable():
    for reason in ("limit", "none", "failed", "battery"):
        out = rewrite_text(StubSupervisor(AiUnavailable(reason)), TEXT, "shorter", CTX)
        assert out == RewriteOutcome(None, False, reason)
    assert rewrite_text(StubSupervisor(BackendError("cancelled")), TEXT, "shorter", CTX) == \
        RewriteOutcome(None, False, "cancelled")
    assert rewrite_text(StubSupervisor(BackendError("timeout")), TEXT, "shorter", CTX) == \
        RewriteOutcome(None, False, "failed")


# ---------------------------------------------------------------- chips


def test_chips_come_from_profile(rig):
    assert rig.ai.tone_chips() == ()  # no field
    rig.open()
    chips = rig.ai.tone_chips()
    assert [(c.key, c.hotkey) for c in chips] == [
        ("friendlier", 1), ("more_direct", 2), ("shorter", 3), ("professional", 4),
        ("custom", None)]
    assert [c.label for c in chips] == ["Friendlier", "More direct", "Shorter", "Professional",
                                        "Rewrite as…"]
    assert rig.panel.tone_chips == chips and rig.panel.profile_label == "Team channel"
    assert ("dm", "Direct message") in rig.panel.profiles
    rig.open(prompt_box())
    assert [c.key for c in rig.ai.tone_chips()] == ["sharpen_prompt", "add_structure",
                                                    "shorter", "custom"]
    assert rig.panel.profile_label == "AI prompt"


def test_ai_profile_offers_sharpen_first(rig):
    rig.open(prompt_box())
    first = rig.ai.tone_chips()[0]
    assert (first.key, first.hotkey) == ("sharpen_prompt", 1)
    rig.ai.select_chip(1)  # Super+Alt+1: selected and made, not applied
    assert [c.tone for c in rig.rewrites()] == ["sharpen_prompt"]
    assert rig.capture.applies == [] and rig.panel.preview_label == "Sharpen prompt"


def test_no_rewrite_call_without_a_click(rig):
    rig.open()
    rig.clock.advance(60_000)
    assert rig.rewrites() == [] and rig.rewrites(rig.spec) == []
    assert rig.panel.preview is None and rig.panel.preview_state is None
    assert rig.panel.can_apply is False


# ---------------------------------------------------------------- preview and apply


def test_selected_tone_streams_into_suggested_then_apply_types_the_whole_field(held):
    s = held.open()
    seen = []
    show = held.overlay.show_panel
    held.overlay.show_panel = lambda view, x, y, **kw: (seen.append(view), show(view, x, y, **kw))
    held.ai.select("friendlier")
    assert held.panel.preview_state == "loading" and held.panel.preview is None
    assert held.panel.can_apply is False
    held.backend.finish(["Thanks ", "so much!"])
    assert held.panel.preview_state == "ready" and held.panel.can_apply is True
    assert held.panel.preview == word_diff(s.text, "Thanks so much!")
    # The first delta was drawn while the rest was still on its way.
    assert ("loading", word_diff(s.text, "Thanks ")) in [(v.preview_state, v.preview)
                                                         for v in seen]
    (call,) = held.rewrites()
    assert (call.text, call.tone, call.ctx.profile) == (s.text, "friendlier", "team_channel")
    held.ai.apply()
    r = held.capture.applies[-1]
    assert r.edit == Edit(0, len(s.text), "Thanks so much!")
    assert r.expected_sha256 == text_sha256(s.text) and r.field_id == "f1"
    assert r.method == "paste"  # one undo step for the whole text, whatever the editor
    assert len(held.capture.applies) == 1


@pytest.mark.parametrize("writeback, method", [("auto", "paste"), ("type", "auto"),
                                               ("paste", "auto")])
def test_apply_leaves_the_method_to_a_per_app_writeback_choice(held, writeback, method):
    held.cfg = Config(backend=held.cfg.backend, apps={"slack": AppCfg(writeback=writeback)})
    s = held.open()
    held.ai.preview("friendlier")
    held.backend.finish(["Thanks so much!"])
    held.ai.apply()
    (r,) = held.capture.applies
    assert r.edit == Edit(0, len(s.text), "Thanks so much!") and r.method == method


def test_preview_of_the_same_tone_is_not_started_twice(held):
    held.open()
    held.ai.preview("shorter")
    held.ai.preview("shorter")  # asked for again while on its way
    assert len(held.rewrites()) == 1 and held.backend.cancelled == 0
    held.backend.finish(["Short."])
    held.ai.preview("shorter")
    assert len(held.rewrites()) == 1 and held.panel.preview_state == "ready"


def test_clicking_a_chip_selects_it_and_apply_types_what_was_seen(rig):
    s = rig.open()
    rig.ai.select("shorter")
    assert rig.panel.preview_state == "ready" and rig.capture.applies == []
    assert rig.panel.preview_label == "Shorter" and rig.ai.selected() == "shorter"
    chips = {c.key: c for c in rig.panel.tone_chips}
    assert chips["shorter"].selected and chips["shorter"].preview == "Thanks so much, will do!"
    assert not any(c.selected for k, c in chips.items() if k != "shorter")
    box = suggested_box(rig.panel, None)
    assert box.tone == "Shorter" and box.can_apply and not box.working
    assert "".join(x.text for x in box.segments) == "Thanks so much, will do!"
    # another chip: the selection moves (its rewrite made); the first is kept for this text
    rig.ai.select("friendlier")
    assert rig.ai.selected() == "friendlier" and len(rig.rewrites()) == 2
    rig.ai.select("shorter")
    assert len(rig.rewrites()) == 2  # remembered: not asked for again
    # Apply (Alt+Enter) in the open panel types the selected rewrite
    rig.c.use_strip()
    assert [r.edit for r in rig.capture.applies] == [Edit(0, len(s.text),
                                                          "Thanks so much, will do!")]


def test_clicking_the_selected_chip_again_goes_back_to_the_corrected_text(rig):
    rig.open()
    rig.ai.select("shorter")
    rig.ai.select("shorter")
    assert rig.ai.selected() is None and rig.panel.preview_state is None
    assert not any(c.selected for c in rig.panel.tone_chips)
    assert rig.panel.preview_label is None and rig.capture.applies == []
    rig.c.use_strip()  # nothing selected: Apply is the corrected text's (none here)
    assert rig.capture.applies == []


def test_number_keys_select_like_a_click_and_open_the_panel(rig):
    rig.c.handle_snapshot(snap())
    rig.clock.advance(1200)
    assert rig.panel is None
    rig.ai.select_chip(3)  # Super+Alt+3, the panel closed: it opens, "Shorter" selected
    assert rig.panel is not None and rig.ai.selected() == "shorter"
    assert rig.capture.applies == []
    rig.ai.select_chip(1)  # the panel open: another chip
    assert rig.ai.selected() == "friendlier"
    rig.ai.select_chip(1)  # the same one again: unselected
    assert rig.ai.selected() is None and rig.capture.applies == []


def test_late_rewrite_is_discarded(held):
    held.backend.ignore_cancel = True  # the result beats the cancel
    s = held.open()
    held.ai.preview("shorter")
    held.c.handle_snapshot(snap(text=s.text + " more"))
    held.backend.finish(["Short."])
    assert held.panel is None or held.panel.preview is None
    held.ai.apply()
    held.clock.advance(1200)
    held.ai.apply()
    assert held.capture.applies == []
    # Nor was it kept for the new text: asking again is a new call.
    held.ai.preview("shorter")
    assert len(held.rewrites()) == 2 and held.panel.preview_state == "loading"


def test_typing_cancels_the_rewrite_in_flight(held):
    s = held.open()
    held.ai.preview("shorter")
    held.c.handle_snapshot(snap(text=s.text + " more"))
    assert held.backend.cancelled == 1 and held.backend.holding == 0
    held.bg.settle()
    assert held.panel.preview is None and held.panel.preview_state is None
    assert held.capture.applies == []


def test_rewrite_for_other_field_is_discarded(held):
    held.backend.ignore_cancel = True
    held.open()
    held.ai.preview("shorter")
    held.c.handle_snapshot(snap(field_id="f2"))  # the same words in another field
    held.backend.finish(["Short."])
    held.clock.advance(1200)
    held.c.open_panel()
    assert held.panel.preview is None
    held.ai.apply()
    assert held.capture.applies == []


def test_hotkey_rewrite_survives_the_blur_its_own_key_grab_causes(held):
    # A global hotkey makes the app report a blur and, on release, the same field again.
    s = held.open()
    held.ai.apply("shorter")
    held.c.handle_focus_lost("blur")
    assert held.backend.cancelled == 0 and held.backend.holding == 1
    held.clock.advance(150)
    held.c.handle_snapshot(snap())
    held.clock.advance(0)
    held.backend.finish(["Short."])
    assert [r.edit for r in held.capture.applies] == [Edit(0, len(s.text), "Short.")]
    assert held.capture.applies[0].expected_sha256 == text_sha256(s.text)
    assert len(held.rewrites()) == 1


def test_rewrite_that_finished_while_the_field_was_out_of_sight(held):
    held.open()
    held.ai.apply("shorter")
    held.c.handle_focus_lost("blur")
    held.backend.finish(["Short."])
    assert held.capture.applies == []  # no field: nothing can be typed
    held.c.handle_snapshot(snap())
    held.clock.advance(0)
    assert [r.edit.replacement for r in held.capture.applies] == ["Short."]


def test_rewrite_is_dropped_when_the_field_stays_away(held):
    held.open()
    held.ai.apply("shorter")
    held.c.handle_focus_lost("blur")
    held.clock.advance(2000)
    assert held.backend.cancelled == 1 and held.backend.holding == 0
    held.bg.settle()
    held.c.handle_snapshot(snap())
    held.clock.advance(1200)
    assert held.capture.applies == [] and len(held.rewrites()) == 1


def test_rewrite_finishing_after_focus_left_is_discarded(held):
    held.backend.ignore_cancel = True  # the result beats every cancel
    held.open()
    held.ai.apply("shorter")
    held.c.handle_focus_lost("blur")
    held.clock.advance(2001)
    held.backend.finish(["Short."])
    held.c.handle_snapshot(snap())
    held.clock.advance(1200)
    assert held.capture.applies == []
    assert held.overlay.panel is None or held.overlay.panel.preview is None


def test_rewrite_is_dropped_when_another_text_or_field_comes_back(held):
    held.backend.ignore_cancel = True
    for back in (snap(text=TEXT + " and more"), snap(field_id="f2")):
        held.c.handle_snapshot(snap())
        held.clock.advance(1200)
        held.ai.apply("shorter")
        held.c.handle_focus_lost("blur")
        held.c.handle_snapshot(back)
        held.clock.advance(0)
        held.backend.finish(["Short."])
        held.clock.advance(1200)
        assert held.capture.applies == []
        held.c.handle_focus_lost("blur")
        held.clock.advance(3000)


def test_pause_stops_a_rewrite_even_if_the_field_comes_back(held):
    held.open()
    held.ai.apply("shorter")
    held.c.toggle_pause()
    assert held.backend.cancelled == 1
    held.bg.settle()
    held.c.resume()
    held.clock.advance(1200)
    assert held.capture.applies == [] and held.backend.holding == 0


def test_second_preview_cancels_first(held):
    s = held.open()
    held.ai.preview("shorter")
    held.ai.preview("professional")
    assert held.backend.cancelled == 1
    assert held.panel.preview_state == "loading"
    held.backend.finish(["Pro."])
    assert held.panel.preview == word_diff(s.text, "Pro.")
    assert [c.tone for c in held.rewrites()] == ["shorter", "professional"]
    held.ai.apply()
    assert [r.edit.replacement for r in held.capture.applies] == ["Pro."]


def test_secret_restored_in_applied_text(tmp_path):
    text = f"use {SECRET} for the api and then tell the team about it"
    rig = Rig(tmp_path, deltas=("Please use [[GLANCE_S1]] for the API, then tell the team.",))
    rig.open(snap(text))
    rig.ai.preview("professional")
    (call,) = rig.rewrites()
    assert SECRET not in call.text and "[[GLANCE_S1]]" in call.text
    rig.ai.apply()
    (r,) = rig.capture.applies
    assert r.edit == Edit(0, len(text), f"Please use {SECRET} for the API, then tell the team.")


def test_dropped_placeholder_blocks_apply_offers_copy(tmp_path):
    cleaned = "Hey, could you look at the release notes before the meeting?"
    rig = Rig(tmp_path, deltas=(cleaned,))
    rig.open(plain(MENTION))
    rig.ai.preview("friendlier")
    assert rig.panel.preview == word_diff(MENTION, cleaned)
    assert rig.panel.can_apply is False and rig.panel.copy_offer == cleaned
    assert rig.panel.banner
    rig.ai.apply()
    rig.ai.apply("friendlier")
    assert rig.capture.applies == []
    assert rig.ai.copy_rewrite() is True and rig.overlay.clipboard == [cleaned]


def test_embedded_object_blocks_apply_offers_copy(tmp_path):
    text = "see ￼ and tell me what you think about it today"
    rig = Rig(tmp_path, deltas=("Please look at [[GLANCE_E1]] and share your thoughts.",))
    rig.open(plain(text))
    rig.ai.preview("friendlier")
    want = "Please look at ￼ and share your thoughts."
    assert rig.panel.preview == word_diff(text, want) and rig.panel.preview_state == "ready"
    assert rig.panel.can_apply is False and rig.panel.copy_offer == want
    rig.ai.apply()
    assert rig.capture.applies == []


def test_the_number_key_s_grab_blur_keeps_the_panel_and_the_selection(rig):
    s = snap()
    rig.c.handle_snapshot(s)
    rig.clock.advance(1200)
    rig.ai.select_chip(3)
    rig.c.handle_focus_lost("blur")  # X sends the app a FocusOut for the key grab
    rig.c.handle_snapshot(s)
    rig.clock.advance(0)
    assert rig.panel is not None and rig.ai.selected() == "shorter"
    rig.ai.select("friendlier")
    rig.ai.select("shorter")  # made before the blur: not asked for again
    assert [c.tone for c in rig.rewrites()] == ["shorter", "friendlier"]
    rig.c.use_strip()
    assert [r.edit.replacement for r in rig.capture.applies] == ["Thanks so much, will do!"]


def test_a_real_focus_change_still_closes_the_panel(rig):
    rig.open()
    rig.ai.select("shorter")
    rig.c.handle_focus_lost("window")
    rig.clock.advance(3000)
    rig.c.handle_snapshot(snap())
    rig.clock.advance(0)
    assert rig.overlay.panel is None


def test_mention_in_a_rich_editor_blocks_apply_offers_copy(tmp_path):
    kept = "Hey [[GLANCE_E1]], could you look at the release notes before the meeting?"
    want = kept.replace("[[GLANCE_E1]]", "@sam")
    rig = Rig(tmp_path, deltas=(kept,))
    rig.open(snap(MENTION))  # Slack: the mention is a pill, typing it back would flatten it
    rig.ai.select_chip(1)
    rig.c.use_strip()  # Apply
    assert rig.capture.applies == []
    assert rig.panel.can_apply is False and rig.panel.copy_offer == want
    assert "Copy" in rig.panel.banner
    # The same text in a plain editor is applied with the mention restored.
    rig.open(plain(MENTION, field_id="f2"))
    rig.ai.select_chip(1)
    rig.c.use_strip()
    assert [r.edit.replacement for r in rig.capture.applies] == [want]


def test_wrapper_output_cleaned_before_apply(tmp_path):
    rig = Rig(tmp_path, deltas=('"Thanks!"',))
    rig.open()
    rig.ai.preview("shorter")
    rig.ai.apply()
    assert rig.capture.applies[-1].edit.replacement == "Thanks!"


def test_unchanged_rewrite_shows_no_change(tmp_path):
    rig = Rig(tmp_path, deltas=(TEXT,))
    rig.open()
    rig.ai.preview("shorter")
    assert rig.panel.banner == "No change suggested"
    assert rig.panel.can_apply is False and rig.panel.copy_offer is None
    rig.ai.apply()
    assert rig.capture.applies == []
    rig.ai.preview("shorter")  # remembered: not asked for again
    assert len(rig.rewrites()) == 1


def test_hotkey_apply_runs_then_applies(held):
    s = held.c.current or snap()
    held.c.handle_snapshot(s)
    held.clock.advance(1200)  # panel closed
    held.ai.apply("shorter")
    assert len(held.rewrites()) == 1 and held.capture.applies == []
    assert held.overlay.chip.state is ChipState.SPINNER and held.overlay.panel is None
    held.backend.finish(["Short."])
    assert [r.edit for r in held.capture.applies] == [Edit(0, len(s.text), "Short.")]
    assert len(held.rewrites()) == 1 and held.overlay.panel is None


def test_hotkey_for_a_chip_the_profile_does_not_have_does_nothing(rig):
    rig.open(prompt_box())  # three chips
    rig.ai.select_chip(4)
    rig.ai.select_chip(0)
    assert rig.rewrites() == [] and rig.capture.applies == []


def test_number_key_for_a_rewrite_that_cannot_apply_shows_the_copy(tmp_path):
    rig = Rig(tmp_path, deltas=("Hey, look at the notes?",))
    rig.c.handle_snapshot(plain(MENTION))
    rig.clock.advance(1200)
    rig.ai.select_chip(1)
    rig.c.use_strip()
    assert rig.capture.applies == []
    assert rig.panel is not None and rig.panel.copy_offer == "Hey, look at the notes?"


def test_custom_tone_passes_instruction(rig):
    rig.open()
    rig.ai.preview("custom", "as a pirate")
    (call,) = rig.rewrites()
    assert (call.tone, call.custom) == ("custom", "as a pirate")
    rig.ai.preview("custom", "as a poet")  # another instruction is another rewrite
    assert [c.custom for c in rig.rewrites()] == ["as a pirate", "as a poet"]
    rig.ai.preview("custom")  # no instruction: nothing to do
    rig.ai.preview("custom", "   ")
    assert len(rig.rewrites()) == 2


def test_explicit_rewrite_at_limit_shows_banner(rig):
    rig.open()
    rig.use_up_budget()
    rig.ai.preview("shorter")
    assert rig.panel.banner == "AI paused (limit)"
    assert rig.panel.preview is None and rig.panel.can_apply is False
    rig.ai.apply()
    rig.ai.apply("shorter")
    assert rig.capture.applies == [] and rig.rewrites() == []


def test_failed_rewrite_shows_banner_and_can_be_retried(rig):
    rig.open()
    rig.backend.script(BackendError("timeout"))
    rig.ai.preview("shorter")
    assert rig.panel.banner and rig.panel.preview_state == "failed"
    rig.ai.preview("shorter")
    assert rig.panel.preview_state == "ready" and rig.panel.banner is None


def test_reverted_rewrite_offers_copy(rig):
    rig.open()
    rig.ai.preview("shorter")
    rig.ai.apply()
    r = rig.capture.applies[-1]
    rig.c.handle_apply_result(ApplyResult(r.apply_id, "reverted", ""))
    assert rig.panel.copy_offer == "Thanks so much, will do!"
    assert rig.ai.copy_rewrite() is True
    assert rig.overlay.clipboard == ["Thanks so much, will do!"]
    assert rig.panel.copy_offer is None and rig.ai.copy_rewrite() is False


def test_reverted_rewrite_is_still_offered_after_the_hotkeys_blur(rig):
    rig.c.handle_snapshot(snap())
    rig.clock.advance(1200)
    rig.ai.select_chip(3)
    rig.c.use_strip()  # Alt+Enter
    r = rig.capture.applies[-1]
    rig.c.handle_focus_lost("blur")  # the key grab
    rig.c.handle_apply_result(ApplyResult(r.apply_id, "reverted", ""))
    rig.c.handle_snapshot(snap())
    rig.clock.advance(1200)
    rig.c.open_panel()
    assert rig.panel.copy_offer == "Thanks so much, will do!"
    rig.c.handle_snapshot(snap(field_id="f2"))  # another field: the offer is gone
    rig.clock.advance(1200)
    rig.c.open_panel()
    assert rig.panel.copy_offer is None and rig.ai.copy_rewrite() is False


def test_verified_rewrite_leaves_no_offer(rig):
    s = rig.open()
    rig.ai.preview("shorter")
    rig.ai.apply()
    r = rig.capture.applies[-1]
    rig.c.handle_apply_result(ApplyResult(r.apply_id, "verified", ""))
    rig.c.handle_snapshot(snap(text="Thanks so much, will do!"))
    rig.clock.advance(1200)
    assert rig.panel is None or (rig.panel.copy_offer is None and rig.panel.preview is None)
    assert s.text != "Thanks so much, will do!"


def test_panel_banner_counts_blanks(tmp_path):
    out = "Goal: fix the bug in [which file?]. Output format: [what format?]"
    rig = Rig(tmp_path, deltas=(out,))
    rig.open(prompt_box("fix the bug and give me the result in the right format please"))
    rig.ai.preview("sharpen_prompt")
    assert rig.panel.banner == "2 blank(s) to fill in after applying"
    assert rig.panel.can_apply is True
    rig.ai.preview("add_structure")  # the blanks only count for the sharpener
    assert rig.panel.banner is None


# ---------------------------------------------------------------- speculation


def test_speculation_only_when_enabled(tmp_path):
    rig = Rig(tmp_path, choice="api")
    rig.c.handle_snapshot(snap())
    rig.clock.advance(2999)
    assert rig.rewrites(rig.spec) == []
    rig.clock.advance(1)
    # The first two chips of the profile, on the speculative instance only.
    assert [c.tone for c in rig.rewrites(rig.spec)] == ["friendlier", "more_direct"]
    assert all(c.text == TEXT for c in rig.rewrites(rig.spec)) and rig.rewrites() == []

    cli = Rig(tmp_path / "cli", choice="claude")
    cli.c.handle_snapshot(snap())
    cli.clock.advance(60_000)
    assert cli.rewrites(cli.spec) == [] and cli.rewrites() == []


def test_speculation_follows_the_config_toggle_and_the_battery(tmp_path):
    on = Rig(tmp_path / "on", choice="claude", speculate=True)
    on.c.handle_snapshot(snap())
    on.clock.advance(3000)
    assert len(on.rewrites(on.spec)) == 2
    off = Rig(tmp_path / "off", choice="api", speculate=False)
    off.c.handle_snapshot(snap())
    off.clock.advance(3000)
    assert off.rewrites(off.spec) == []
    battery = Rig(tmp_path / "battery", choice="api")
    battery.battery = True
    battery.c.handle_snapshot(snap())
    battery.clock.advance(3000)
    assert battery.rewrites(battery.spec) == []


def test_typing_restarts_the_speculation_wait(tmp_path):
    rig = Rig(tmp_path, choice="api")
    rig.c.handle_snapshot(snap())
    rig.clock.advance(2000)
    rig.c.handle_snapshot(snap(text=TEXT + " and more words"))
    rig.clock.advance(2999)
    assert rig.rewrites(rig.spec) == []
    rig.clock.advance(1)
    assert [c.text for c in rig.rewrites(rig.spec)] == [TEXT + " and more words"] * 2


def test_pregenerated_apply_is_immediate(tmp_path):
    rig = Rig(tmp_path, choice="api", hold=True)
    rig.spec.hold = False
    s = rig.open()
    rig.clock.advance(1800)
    assert len(rig.rewrites(rig.spec)) == 2
    rig.ai.apply("friendlier")
    assert [r.edit for r in rig.capture.applies] == [Edit(0, len(s.text), "Speculated.")]
    assert rig.rewrites() == [] and rig.backend.holding == 0  # no call was needed


def test_pregenerated_preview_shows_at_once(tmp_path):
    rig = Rig(tmp_path, choice="api")
    s = rig.open()
    rig.clock.advance(1800)
    rig.ai.preview("more_direct")
    assert rig.panel.preview_state == "ready"
    assert rig.panel.preview == word_diff(s.text, "Speculated.") and rig.rewrites() == []


# ---------------------------------------------------------------- "Rewrite as…"


def test_rewrite_as_fills_suggested_once_the_field_is_back_and_apply_types_it(rig):
    s = rig.open()
    rig.ai.select("custom")
    ((prompt, answer),) = rig.overlay.asked
    assert prompt == "Rewrite as…" and rig.rewrites() == []
    rig.c.handle_focus_lost("window")  # the entry window took the focus
    answer("as a pirate", True)  # the overlay saw the target window active again
    assert rig.rewrites() == []  # but its field has not been seen yet
    rig.c.handle_snapshot(snap())
    rig.clock.advance(0)
    (call,) = rig.rewrites()
    assert (call.tone, call.custom) == ("custom", "as a pirate")
    assert rig.capture.applies == []  # in "Suggested" first, selected
    assert rig.panel.preview_label == "Your rewrite" and rig.panel.can_apply
    assert [c.selected for c in rig.panel.tone_chips if c.key == "custom"] == [True]
    rig.c.use_strip()  # Apply (Alt+Enter)
    (r,) = rig.capture.applies
    assert r.edit == Edit(0, len(s.text), "Thanks so much, will do!")
    assert r.expected_sha256 == text_sha256(s.text)


def test_rewrite_as_does_not_apply_when_the_window_did_not_come_back(rig):
    rig.open()
    rig.ai.ask_custom()
    rig.c.handle_focus_lost("window")
    rig.overlay.asked[-1][1]("as a pirate", False)  # activation timed out
    rig.clock.advance(5000)
    rig.c.handle_snapshot(snap())  # the user went back by hand
    rig.clock.advance(0)
    assert rig.capture.applies == []
    assert rig.panel.can_apply is False and rig.panel.copy_offer == "Thanks so much, will do!"
    rig.ai.apply()
    assert rig.capture.applies == []


def test_rewrite_as_is_dropped_when_the_text_changed_meanwhile(rig):
    rig.open()
    rig.ai.ask_custom()
    rig.c.handle_focus_lost("window")
    rig.overlay.asked[-1][1]("as a pirate", True)
    rig.c.handle_snapshot(snap(text=TEXT + " typed meanwhile"))
    rig.clock.advance(0)
    assert rig.rewrites() == [] and rig.capture.applies == []
    rig.c.handle_snapshot(snap())  # even if the old text comes back later
    rig.clock.advance(0)
    assert rig.rewrites() == []


def test_rewrite_as_is_dropped_when_the_field_takes_too_long(rig):
    rig.open()
    rig.ai.ask_custom()
    rig.c.handle_focus_lost("window")
    rig.overlay.asked[-1][1]("as a pirate", True)
    rig.clock.advance(2001)
    rig.c.handle_snapshot(snap())
    rig.clock.advance(0)
    assert rig.rewrites() == [] and rig.capture.applies == []


def test_rewrite_as_ignores_an_empty_answer_and_a_missing_field(rig):
    rig.ai.ask_custom()  # no field: nothing to ask about
    assert rig.overlay.asked == []
    rig.open()
    rig.ai.ask_custom()
    rig.overlay.asked[-1][1]("   ", True)
    rig.clock.advance(0)
    assert rig.rewrites() == []


# ---------------------------------------------------------------- the drawer's tone cards


def test_cards_carry_their_rewrite_once_there_is_one(held):
    held.open()
    chips = {c.key: c for c in held.panel.tone_chips}
    assert all(c.preview is None and c.state is None for c in chips.values())
    held.ai.preview("friendlier")
    assert {c.key: c.state for c in held.panel.tone_chips}["friendlier"] == "loading"
    held.backend.finish(["Thanks ", "so much!"])
    chips = {c.key: c for c in held.panel.tone_chips}
    assert (chips["friendlier"].preview, chips["friendlier"].state) == ("Thanks so much!",
                                                                        "ready")
    held.ai.preview("shorter")  # the friendlier one stays known for this text
    chips = {c.key: c for c in held.panel.tone_chips}
    assert chips["friendlier"].preview == "Thanks so much!"
    assert chips["shorter"].state == "loading" and chips["custom"].preview is None
    assert [c.tone for c in held.rewrites()] == ["friendlier", "shorter"]
