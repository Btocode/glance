import pytest

from glance.audience.profiles import BUILTIN
from glance.audience.resolver import DEFAULT_RULES, AudienceResolver
from glance.backends.base import BackendError, BaseBackend, Ctx
from glance.backends.budget import Budget
from glance.backends.prompts import complete_system
from glance.backends.supervisor import AiUnavailable, BackendSupervisor
from glance.config import BackendCfg, Config, GeneralCfg, UiCfg, load_config
from glance.daemon.controller import Controller
from glance.features.autocomplete import Autocomplete, clean_completion
from glance.grammar.filters import IssueFilter
from glance.grammar.store import IgnoredRules, PersonalDictionary
from glance.protocol import ApplyResult, Edit, FieldSnapshot, Rect, text_sha256
from glance.ui.hotkeys import parse_chord
from tests.fakes import (
    FakeBackend,
    FakeBg,
    FakeCapture,
    FakeClock,
    FakeGhost,
    FakeHarper,
    FakeHotkeys,
    FakeNotifier,
    FakeOverlay,
    FakeTray,
    HeldBg,
)

TEXT = "I will send it"
LONG = "I will send it to you"  # long enough for a live completion
SECRET = "sk-ant-api03-AbCdEf0123456789xyzQRSTUV"
CTX = Ctx(profile="dm", target_tone="casual", strictness="relaxed", app_label="Slack")
CARET = Rect(100, 200, 1, 20)
FIELD = Rect(90, 190, 400, 300)


def snap(text=TEXT, caret=None, field_id="f1", gated=None, caret_rect=CARET, field_rect=FIELD):
    return FieldSnapshot(
        app="Slack", wm_class="Slack", title="Sam Lee (DM) - Acme - Slack", url=None,
        role="text", text=text, caret=len(text) if caret is None else caret,
        caret_rect=caret_rect, prev_char_rect=None, field_rect=field_rect,
        window_rect=Rect(50, 50, 800, 600), field_id=field_id, capture_confidence=1.0, seq=1,
        pid=100, gated=gated)


class NoOverrides:
    def get(self, key):
        return None


class Rig:
    """Controller + supervisor + Autocomplete on fakes.

    `held=True` keeps every background job pending until `finish()`.
    """

    def __init__(self, tmp_path, choice="claude", live=None, held=False, language=None):
        self.clock = FakeClock()
        self.cfg = Config(backend=BackendCfg(choice=choice, live_autocomplete=live))
        self.battery = False
        self.backend = FakeBackend(choice)
        self.spec = FakeBackend(choice)  # the instance speculative (live) calls run on
        now = FakeBg(self.clock, self.backend, self.spec)
        self.bg = HeldBg() if held else now
        self.capture = FakeCapture()
        self.ghost = FakeGhost()
        self.hotkeys = FakeHotkeys()
        self.c = Controller(
            self.clock, lambda: self.cfg, FakeHarper(),
            IssueFilter(PersonalDictionary(tmp_path / "dictionary.txt"),
                        IgnoredRules(tmp_path / "ignored.json"), []),
            FakeOverlay(), self.capture, FakeNotifier(), FakeTray(), now)
        self.budget = Budget(self.clock, lambda: self.cfg.backend, tmp_path / "usage.json")
        self.sup = BackendSupervisor(
            {choice: self.backend}, lambda: self.cfg.backend, self.budget, self.clock,
            lambda: self.battery, lambda status: None, speculative={choice: self.spec})
        self.ac = Autocomplete(
            self.c, self.sup, AudienceResolver(BUILTIN, DEFAULT_RULES, NoOverrides()),
            self.ghost, self.hotkeys, self.clock, self.bg, lambda: self.cfg,
            language or (lambda text: False))
        self.ac.attach()

    def completes(self):
        return [call for b in (self.backend, self.spec) for call in b.calls
                if call.kind == "complete"]

    def finish(self, deltas):
        """Let the pending completion end with `deltas` (held rigs only)."""
        self.backend.finish_complete(deltas)
        self.bg.run()

    def show(self, text=TEXT, completion=" tomorrow morning."):
        """Put a suggestion on screen through the on-demand path."""
        self.c.handle_snapshot(snap(text))
        self.backend.finish_complete([completion])
        self.ac.trigger()
        if isinstance(self.bg, HeldBg):
            self.bg.run()
        assert self.ac.visible and self.ghost.shown.text == completion

    def typed(self, before, after):
        """The user types: `before` is in the field, then `after`."""
        self.c.handle_snapshot(snap(before))
        self.c.handle_snapshot(snap(after))

    def use_up_budget(self):
        while self.budget.try_spend():
            pass

    def ac_grabs(self):
        return {"ac_tab", "ac_word"} & set(self.hotkeys.grabbed())


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


# ---------------------------------------------------------------- clean_completion


@pytest.mark.parametrize("raw, prefix, want", [
    ("tomorrow morning.", "I will send it", " tomorrow morning."),
    (" tomorrow morning.", "I will send it ", "tomorrow morning."),
    ("I will send it tomorrow.", "I will send it", " tomorrow."),  # echoed prefix
    ("tomorrow. Then we can talk.", "see you", " tomorrow."),  # one sentence
    ("first line\nsecond", "ok", " first line"),
    ('"by Friday"', "done", " by Friday"),
    ("use [[GLANCE_S1]] here", "the key is", None),
    ("   ", "x", None),
])
def test_clean_completion(raw, prefix, want):
    assert clean_completion(raw, prefix) == want


@pytest.mark.parametrize("raw, prefix, want", [
    ("", "x", None),
    ("```\nby Friday\n```", "done", " by Friday"),
    ("\n\ntomorrow", "see you", " tomorrow"),
    ("  tomorrow  ", "see you", " tomorrow"),  # exactly one space
    (", thanks!", "ok", ", thanks!"),  # punctuation hugs the word before it
    ("thanks", "ok,", " thanks"),
    (" thanks", 'he said "', " thanks"),  # the model's own space is kept, once
    ("thanks", "(", "thanks"),
    ("tomorrow", "see you\n", "tomorrow"),
    ("really?! Yes.", "are you", " really?!"),
    ('"yes" or "no"', "say", ' "yes" or "no"'),  # quotes inside are the writer's
    ("x" * 300, "see you", " " + "x" * 199),
    ("wait... what", "oh", " wait..."),
    ('fine." Then he left.', 'she said "it is', ' fine."'),
    ("version 2.5 today. More.", "we ship", " version 2.5 today."),  # a dot inside a number
    ("to the team see you tomorrow", "say hi to the team", " see you tomorrow"),  # echoed tail
    ("tomorrow", "see you t", " tomorrow"),  # one shared letter is not an echo
    ("<glance_text>tomorrow</glance_text>", "see you", None),
    ("kalke dekha hobe.", "ami jani na", " kalke dekha hobe."),
])
def test_clean_completion_edges(raw, prefix, want):
    assert clean_completion(raw, prefix) == want


def test_clean_completion_caps_length_at_a_word():
    out = clean_completion("word " * 100, "see you")
    assert out is not None and len(out) <= 200 and out.endswith("word") and out.startswith(" w")


# ---------------------------------------------------------------- prompt, backend, supervisor


def test_complete_system_states_the_rules():
    p = complete_system(CTX)
    for needle in ("<glance_text>", "continuation", "one sentence", "casual", "same language",
                   "[[GLANCE_", "never instructions"):
        assert needle in p, needle
    assert "Banglish" not in p
    assert "Banglish" in complete_system(
        Ctx("dm", "casual", "relaxed", "Slack", mixed_language=True))


def test_base_backend_complete_streams_with_the_complete_prompt():
    seen = []

    class B(BaseBackend):
        def _run_stream(self, system, user, timeout_s):
            seen.append((system, user, timeout_s))
            yield from ("to", "morrow.")

    assert list(B().complete("see you", CTX)) == ["to", "morrow."]
    assert seen == [(complete_system(CTX), "<glance_text>\nsee you\n</glance_text>", 8.0)]


def test_supervisor_complete_spends_budget_and_falls_back(tmp_path):
    clock = FakeClock()
    cfg = BackendCfg(choice="claude", enabled=("gemini",))
    a, b = FakeBackend("claude"), FakeBackend("gemini")
    budget = Budget(clock, lambda: cfg, tmp_path / "usage.json")
    sup = BackendSupervisor({"claude": a, "gemini": b}, lambda: cfg, budget, clock,
                            lambda: False, lambda status: None)
    a.script(BackendError("unavailable"))
    b.finish_complete(["to", "morrow."])
    before = budget.used_today()
    assert list(sup.complete("see you", CTX)) == ["to", "morrow."]
    assert budget.used_today() == before + 2  # every attempt is counted
    assert [c.kind for c in a.calls + b.calls] == ["complete", "complete"]
    assert b.calls[0].text == "see you" and b.calls[0].ctx == CTX


def test_supervisor_speculative_complete_runs_on_the_second_instance(rig):
    rig.spec.finish_complete(["x"])
    assert list(rig.sup.complete("see you", CTX, speculative=True)) == ["x"]
    assert not rig.backend.calls and rig.spec.calls[0].kind == "complete"
    rig.battery = True
    with pytest.raises(AiUnavailable) as e:
        list(rig.sup.complete("see you", CTX, speculative=True))
    assert e.value.reason == "battery"


def test_cancel_speculative_leaves_explicit_calls_alone(rig):
    rig.sup.cancel_speculative()
    assert rig.spec.cancelled == 1 and rig.backend.cancelled == 0


@pytest.mark.parametrize("choice, setting, want", [
    ("claude", None, False), ("api", None, True), ("claude", True, True), ("api", False, False),
])
def test_live_autocomplete_default_follows_the_backend(tmp_path, choice, setting, want):
    assert Rig(tmp_path, choice=choice, live=setting).sup.live_autocomplete() is want


def test_config_defaults_and_overrides(tmp_path):
    cfg = Config()
    assert cfg.general.hotkeys["complete"] == "Super+Alt+Return"
    parse_chord(cfg.general.hotkeys["complete"])
    assert cfg.general.autocomplete_idle_ms == 600 and cfg.ui.ghost_font_factor == 0.81
    p = tmp_path / "config.toml"
    p.write_text("[general]\nautocomplete_idle_ms = 900\n[ui]\nghost_font_factor = 0.8\n"
                 "[backend]\nlive_autocomplete = true\n")
    cfg = load_config(p)
    assert cfg.general.autocomplete_idle_ms == 900 and cfg.ui.ghost_font_factor == 0.8
    assert cfg.backend.live_autocomplete is True
    p.write_text("[ui]\nghost_font_factor = 1\n")
    assert load_config(p).ui.ghost_font_factor == 1.0
    p.write_text('[ui]\nghost_font_factor = "big"\n')
    with pytest.raises(Exception, match="ghost_font_factor"):
        load_config(p)


# ---------------------------------------------------------------- on demand


def test_on_demand_shows_ghost_and_grabs_keys(tmp_path):
    rig = Rig(tmp_path, held=True)
    rig.c.handle_snapshot(snap())
    rig.ac.trigger()
    assert not rig.ac.visible and rig.ghost.shown is None
    rig.finish(["tomorrow ", "morning."])
    assert rig.ghost.shown.text == " tomorrow morning."
    assert rig.ac.visible and rig.hotkeys.grabbed().items() >= {
        "ac_tab": "Tab", "ac_word": "Control+Right"}.items()
    call, = rig.backend.calls
    assert (call.kind, call.text) == ("complete", TEXT)
    assert (call.ctx.profile, call.ctx.app_label, call.ctx.mixed_language) == ("dm", "Slack", False)
    assert not rig.spec.calls  # explicit calls never run on the speculative instance


def test_ghost_sits_after_the_caret(rig):
    rig.show()
    g = rig.ghost.shown
    # x = caret right edge, y = caret top, font = caret height * 0.81, width = field right - x
    assert (g.x, g.y, g.font_px, g.max_width) == (101, 200, 16, 390)


def test_status_reports_whether_a_suggestion_is_visible(rig):
    def status():
        return rig.c.status()["autocomplete_visible"]

    assert status() is False
    rig.show()
    assert status() is True
    rig.ac.dismiss()
    assert status() is False


def test_ghost_font_follows_config(rig):
    rig.cfg = Config(backend=rig.cfg.backend, ui=UiCfg(ghost_font_factor=0.5))
    rig.show()
    assert rig.ghost.shown.font_px == 10


def test_ghost_without_a_field_rect_uses_the_window(rig):
    rig.c.handle_snapshot(snap(field_rect=None))
    rig.ac.trigger()
    assert rig.ghost.shown.max_width == 850 - 100


def test_no_grab_when_nothing_visible(rig):
    assert not rig.ac_grabs()
    rig.c.handle_snapshot(snap())
    assert not rig.ac_grabs()
    rig.backend.finish_complete(["   "])  # cleans to None
    rig.ac.trigger()
    assert len(rig.completes()) == 1
    assert not rig.ac.visible and rig.ghost.shown is None and not rig.ac_grabs()


def test_backend_failure_shows_nothing(rig):
    rig.c.handle_snapshot(snap())
    rig.backend.script(BackendError("timeout"))
    rig.ac.trigger()
    assert not rig.ac.visible and not rig.ac_grabs()


def test_tab_that_cannot_be_grabbed_shows_nothing(rig):
    rig.hotkeys.taken = {"Tab"}
    rig.c.handle_snapshot(snap())
    rig.ac.trigger()
    assert len(rig.completes()) == 1
    assert not rig.ac.visible and rig.ghost.shown is None and not rig.ac_grabs()


def test_grabbed_keys_accept(rig):
    rig.show(completion=" tomorrow morning.")
    rig.hotkeys.press("ac_tab")
    assert rig.capture.applies[-1].edit == Edit(14, 14, " tomorrow morning.")
    rig.c.handle_apply_result(ApplyResult(rig.capture.applies[-1].apply_id, "stale", ""))
    rig.c.handle_focus_lost("blur")
    rig.show(completion=" tomorrow morning.")
    rig.hotkeys.press("ac_word")
    assert rig.capture.applies[-1].edit == Edit(14, 14, " tomorrow")


def test_trigger_replaces_a_visible_suggestion(rig):
    rig.show(completion=" tomorrow.")
    rig.backend.finish_complete(["next week."])
    rig.ac.trigger()
    assert rig.ghost.shown.text == " next week." and rig.ghost.hides == 1


# ---------------------------------------------------------------- hiding


def test_typing_dismisses_and_ungrabs_and_cancels(tmp_path):
    rig = Rig(tmp_path, held=True)
    rig.show()
    rig.c.handle_snapshot(snap(TEXT + "x"))
    assert rig.ghost.shown is None and not rig.ac.visible and not rig.ac_grabs()

    rig.c.handle_snapshot(snap(TEXT + "xy"))
    rig.ac.trigger()  # pending now
    assert rig.backend.cancelled == 0
    rig.c.handle_snapshot(snap(TEXT + "xyz"))
    assert rig.backend.cancelled == 1  # supervisor.cancel_all()
    rig.finish(["too late."])
    assert rig.ghost.shown is None and not rig.ac_grabs()


def test_caret_move_dismisses(rig):
    rig.show()
    rig.c.handle_snapshot(snap(TEXT, caret=3))
    assert not rig.ac.visible and not rig.ac_grabs()


def test_same_snapshot_again_keeps_the_suggestion(rig):
    rig.show()
    rig.c.handle_snapshot(snap(TEXT))
    assert rig.ac.visible and rig.ac_grabs() == {"ac_tab", "ac_word"}


def test_grab_released_on_every_hide_path(rig):
    c, ac = rig.c, rig.ac
    paths = (
        lambda: ac.dismiss(),
        lambda: ac.on_focus_lost(),
        lambda: (rig.clock.advance(600), c.handle_focus_lost("blur")),  # not the hotkey's
        lambda: c.toggle_pause(),
        lambda: c.handle_snapshot(snap(gated="password", text="")),
        lambda: ac.accept_all(),
        lambda: ac.accept_word(),
        lambda: c.handle_capture_disconnected(),
        lambda: c.handle_snapshot(snap("something else entirely", field_id="f2")),
        lambda: rig.clock.advance(10_000),
    )
    for path in paths:
        rig.show()
        path()
        assert not ac.visible and rig.ghost.shown is None
        assert "ac_tab" not in rig.hotkeys.grabbed() and "ac_word" not in rig.hotkeys.grabbed()
        # back to a clean, focused, running state for the next path
        c.handle_capture_connected()
        c.resume()
        if rig.capture.applies:
            c.handle_apply_result(ApplyResult(rig.capture.applies[-1].apply_id, "stale", ""))
        c.handle_focus_lost("blur")
    assert rig.hotkeys.log.count(("grab", "ac_tab")) == len(paths)
    assert rig.hotkeys.log.count(("ungrab", "ac_tab")) == len(paths)


def test_watchdog_releases_grab(rig):
    rig.show()
    rig.clock.advance(9_999)
    assert rig.ac.visible
    rig.clock.advance(1)
    assert not rig.ac.visible and "ac_tab" not in rig.hotkeys.grabbed()
    assert "ac_word" not in rig.hotkeys.grabbed()


def test_paused_trigger_does_nothing_and_resume_works(rig):
    rig.c.handle_snapshot(snap())
    rig.c.toggle_pause()
    rig.ac.trigger()
    rig.clock.advance(5_000)
    assert not rig.completes()
    rig.c.resume()  # the controller replays the snapshot
    rig.ac.trigger()
    assert rig.ac.visible


# ---------------------------------------------------------------- accepting


def test_accept_all_inserts_at_caret(rig):
    rig.show(text="I will send it", completion=" tomorrow.")
    rig.ac.accept_all()
    req = rig.capture.applies[-1]
    assert req.edit == Edit(14, 14, " tomorrow.") and req.method == "auto"
    assert req.expected_sha256 == text_sha256("I will send it") and req.field_id == "f1"
    assert not rig.ac.visible
    rig.c.handle_snapshot(snap("I will send it tomorrow."))
    assert not rig.ac.visible and len(rig.capture.applies) == 1


def test_accept_before_a_newline_inserts_at_the_caret(rig):
    text = "I will send it\nsecond line"
    rig.c.handle_snapshot(snap(text, caret=14))
    rig.ac.trigger()
    rig.ac.accept_all()
    assert rig.backend.calls[0].text == "I will send it"
    assert rig.capture.applies[-1].edit == Edit(14, 14, " tomorrow morning.")


def test_accept_with_nothing_visible_does_nothing(rig):
    rig.c.handle_snapshot(snap())
    rig.ac.accept_all()
    rig.ac.accept_word()
    assert not rig.capture.applies


def test_stale_accept_inserts_nothing(rig):
    rig.show()
    rig.ac.accept_all()
    rig.c.handle_apply_result(ApplyResult(rig.capture.applies[-1].apply_id, "stale", ""))
    assert not rig.ac.visible and len(rig.capture.applies) == 1
    assert rig.ghost.shown is None and not rig.ac_grabs()


def test_accept_word_keeps_remainder(rig):
    rig.show(text="see you", completion=" tomorrow morning.")
    rig.ac.accept_word()
    assert rig.capture.applies[-1].edit == Edit(7, 7, " tomorrow")
    # P3-4: nothing is shown or grabbed while the word is being typed into the field
    assert not rig.ac.visible and rig.ghost.shown is None and not rig.ac_grabs()
    rig.c.handle_snapshot(snap(text="see you tom", caret=11))  # the typing, half way
    assert not rig.ac.visible
    rig.c.handle_apply_result(ApplyResult(rig.capture.applies[-1].apply_id, "verified", ""))
    rig.c.handle_snapshot(snap(text="see you tomorrow", caret=16, caret_rect=Rect(180, 200, 1, 20)))
    assert rig.ghost.shown.text == " morning." and "ac_tab" in rig.hotkeys.grabbed()
    assert rig.ghost.shown.x == 181  # re-anchored at the new caret
    assert len(rig.completes()) == 1  # the remainder costs no call
    rig.ac.accept_word()
    req = rig.capture.applies[-1]
    assert req.edit == Edit(16, 16, " morning.")
    assert req.expected_sha256 == text_sha256("see you tomorrow")
    rig.c.handle_apply_result(ApplyResult(req.apply_id, "verified", ""))
    rig.c.handle_snapshot(snap(text="see you tomorrow morning."))
    assert not rig.ac.visible and not rig.ac_grabs()


def test_remainder_has_its_own_watchdog(rig):
    rig.show(text="see you", completion=" tomorrow morning.")
    rig.clock.advance(9_000)
    rig.ac.accept_word()
    rig.c.handle_snapshot(snap(text="see you tomorrow"))
    rig.clock.advance(9_999)
    assert rig.ac.visible
    rig.clock.advance(1)
    assert not rig.ac.visible and not rig.ac_grabs()


def test_accept_word_then_other_text_drops_the_remainder(rig):
    rig.show(text="see you", completion=" tomorrow morning.")
    rig.ac.accept_word()
    rig.c.handle_snapshot(snap(text="see you tomorrox"))
    assert not rig.ac.visible
    rig.c.handle_snapshot(snap(text="see you tomorrow"))
    assert not rig.ac.visible and not rig.ac_grabs()


@pytest.mark.parametrize("status", ["refused", "stale", "failed", "reverted"])
def test_accept_word_refused_while_ctrl_held(rig, status):
    # P3-4: capture refuses to type while Ctrl is still down; the remainder goes too
    rig.show(text="see you", completion=" tomorrow morning.")
    rig.ac.accept_word()
    rig.c.handle_apply_result(ApplyResult(rig.capture.applies[-1].apply_id, status, ""))
    rig.c.handle_snapshot(snap(text="see you tomorrow"))
    assert not rig.ac.visible and rig.ghost.shown is None and not rig.ac_grabs()
    assert len(rig.capture.applies) == 1  # nothing re-tried


def test_accept_word_when_the_apply_cannot_be_sent(rig):
    rig.show(text="see you", completion=" tomorrow morning.")
    rig.capture.connected = False
    rig.ac.accept_word()
    assert not rig.capture.applies and not rig.ac.visible
    rig.capture.connected = True
    rig.c.handle_snapshot(snap(text="see you tomorrow"))
    assert not rig.ac.visible


def test_accept_word_on_a_one_word_suggestion_is_accept_all(rig):
    rig.show(text="see you", completion=" tomorrow.")
    rig.ac.accept_word()
    assert rig.capture.applies[-1].edit == Edit(7, 7, " tomorrow.")
    rig.c.handle_snapshot(snap(text="see you tomorrow."))
    assert not rig.ac.visible


# ---------------------------------------------------------------- live


def test_live_only_when_enabled(tmp_path):
    rig = Rig(tmp_path, choice="claude")
    rig.typed("I will send it", LONG)
    rig.clock.advance(600)
    assert not rig.completes() and not rig.ac.visible

    rig = Rig(tmp_path, choice="api")
    rig.typed("I will send it", LONG)
    rig.clock.advance(599)
    assert not rig.completes()
    rig.clock.advance(1)
    assert len(rig.spec.calls) == 1 and not rig.backend.calls  # the speculative instance
    assert rig.ac.visible and rig.ghost.shown.text == " tomorrow morning."

    rig = Rig(tmp_path, choice="claude", live=True)
    rig.typed("I will send it", LONG)
    rig.clock.advance(600)
    assert len(rig.completes()) == 1 and rig.ac.visible

    rig = Rig(tmp_path, choice="api", live=False)
    rig.typed("I will send it", LONG)
    rig.clock.advance(600)
    assert not rig.completes()


def test_live_waits_for_a_pause(tmp_path):
    rig = Rig(tmp_path, choice="api")
    rig.typed("I will send it", LONG)
    rig.clock.advance(400)
    rig.c.handle_snapshot(snap(TEXT + " t"))
    rig.clock.advance(400)
    assert not rig.completes()
    rig.clock.advance(200)
    assert [c.text for c in rig.completes()] == [TEXT + " t"]


def test_live_idle_time_follows_config(tmp_path):
    rig = Rig(tmp_path, choice="api")
    rig.cfg = Config(backend=rig.cfg.backend, general=GeneralCfg(autocomplete_idle_ms=900))
    rig.typed("I will send it", LONG)
    rig.clock.advance(899)
    assert not rig.completes()
    rig.clock.advance(1)
    assert len(rig.completes()) == 1


def test_live_needs_typing_not_just_focus(tmp_path):
    rig = Rig(tmp_path, choice="api")
    rig.c.handle_snapshot(snap())  # focusing a field that already holds text
    rig.c.handle_snapshot(snap(caret=3))
    rig.c.handle_snapshot(snap())
    rig.clock.advance(5_000)
    assert not rig.completes()


def test_live_calls_count_toward_the_daily_cap(tmp_path):
    rig = Rig(tmp_path, choice="api")
    before = rig.budget.used_today()
    rig.typed("I will send it", LONG)
    rig.clock.advance(600)
    assert rig.budget.used_today() == before + 1


def test_live_respects_auto_allowed(tmp_path):
    rig = Rig(tmp_path, choice="api")
    rig.battery = True
    rig.typed("I will send it", LONG)
    rig.clock.advance(600)
    assert not rig.completes()
    rig.ac.trigger()  # asked for: battery does not matter
    assert len(rig.backend.calls) == 1 and rig.ac.visible

    rig = Rig(tmp_path, choice="api")
    rig.use_up_budget()
    rig.typed("I will send it", LONG)
    rig.clock.advance(600)
    assert not rig.completes()
    rig.ac.trigger()  # the daily cap holds for every call
    assert not rig.completes() and not rig.ac.visible


def test_live_does_not_replace_a_visible_suggestion(tmp_path):
    rig = Rig(tmp_path, choice="api")
    rig.typed("I will send it", LONG)
    rig.ac.trigger()
    assert rig.ac.visible
    rig.clock.advance(600)
    assert len(rig.completes()) == 1


def test_live_cancel_leaves_explicit_calls_alone(tmp_path):
    rig = Rig(tmp_path, choice="api", held=True)
    rig.typed("I will send it", LONG)
    rig.clock.advance(600)  # the live call is pending
    rig.c.handle_snapshot(snap(TEXT + "x"))
    assert rig.spec.cancelled == 1 and rig.backend.cancelled == 0
    rig.bg.run()
    assert not rig.ac.visible


def test_typing_while_live_is_off_makes_no_job(tmp_path):
    rig = Rig(tmp_path, choice="claude", held=True)
    rig.typed("I will send it", LONG)
    rig.clock.advance(600)
    assert rig.bg.pending == 0


# ---------------------------------------------------------------- eligibility, privacy


@pytest.mark.parametrize("s", [
    snap("I will send it to you now", caret=18),  # mid-line
    snap("I will send it to you", caret_rect=None),
    snap("   \n  "),  # nothing to continue
    snap(""),
])
def test_not_eligible_mid_line_or_short_or_no_caret_rect(tmp_path, s):
    rig = Rig(tmp_path, choice="api")
    rig.c.handle_snapshot(snap(s.text + "x", caret=s.caret, caret_rect=s.caret_rect))
    rig.c.handle_snapshot(s)
    rig.clock.advance(600)
    rig.ac.trigger()
    assert not rig.completes() and not rig.ac.visible


def test_live_needs_fifteen_chars_before_the_caret(tmp_path):
    rig = Rig(tmp_path, choice="api")
    rig.typed("x" * 13, "x" * 14)
    rig.clock.advance(600)
    assert not rig.completes()
    rig.typed("x" * 14, "x" * 15)
    rig.clock.advance(600)
    assert len(rig.completes()) == 1

    rig = Rig(tmp_path, choice="api")  # 15 characters in the field, 14 before the caret
    rig.typed("x" * 13 + "\ny", "x" * 14 + "\ny")
    rig.c.handle_snapshot(snap("x" * 14 + "\ny", caret=14))
    rig.clock.advance(600)
    assert not rig.completes()
    rig.ac.trigger()  # asked for: any text will do
    assert len(rig.completes()) == 1


def test_context_is_shielded_and_capped_at_2000(rig):
    text = "word " * 990 + f"the key is {SECRET} and"
    assert len(text) > 4900
    rig.c.handle_snapshot(snap(text + " more text after the caret", caret=len(text)))
    assert rig.c.current.text[len(text)] == " "  # mid-line: not eligible
    rig.c.handle_snapshot(snap(text + "\nmore text after the caret", caret=len(text)))
    rig.ac.trigger()
    sent, = rig.backend.calls
    assert len(sent.text) <= 2000 and sent.text.endswith("the key is [[GLANCE_S1]] and")
    assert SECRET not in sent.text and "more text" not in sent.text


def test_secret_cut_by_the_2000_limit_is_not_sent(rig):
    # the last 2000 characters start in the middle of the key
    text = SECRET + " " + "word " * 397 + "word"
    assert text[-2000:].startswith(SECRET[-10:])
    rig.c.handle_snapshot(snap(text))
    rig.ac.trigger()
    sent, = rig.backend.calls
    assert sent.text == text[len(SECRET):]  # neither a piece of the key nor of its placeholder
    assert not any(SECRET[i:i + 6] in sent.text for i in range(len(SECRET) - 5))


def test_completion_naming_a_placeholder_is_not_shown(rig):
    rig.c.handle_snapshot(snap(f"the key is {SECRET} and"))
    rig.backend.finish_complete(["then [[GLANCE_S1]] again."])
    rig.ac.trigger()
    assert rig.backend.calls[0].text == "the key is [[GLANCE_S1]] and"
    assert not rig.ac.visible and not rig.ac_grabs()


def test_mixed_language_reaches_the_prompt_context(tmp_path):
    seen = []

    def language(text):
        seen.append(text)
        return True

    rig = Rig(tmp_path, language=language)
    rig.c.handle_snapshot(snap("ami kalke office e jabo\nnext line", caret=23))
    rig.ac.trigger()
    assert rig.backend.calls[0].ctx.mixed_language is True
    assert seen == ["ami kalke office e jabo"]


def test_late_completion_for_old_generation_dropped(tmp_path):
    rig = Rig(tmp_path, held=True)
    rig.c.handle_snapshot(snap())
    rig.ac.trigger()
    rig.c.handle_snapshot(snap(TEXT + " to"))
    rig.finish(["morrow."])
    assert not rig.ac.visible and rig.ghost.shown is None and not rig.ac_grabs()

    rig = Rig(tmp_path, held=True)  # same text again in another field
    rig.c.handle_snapshot(snap())
    rig.ac.trigger()
    rig.c.handle_snapshot(snap(field_id="f2"))
    rig.finish(["tomorrow."])
    assert not rig.ac.visible

    rig = Rig(tmp_path, held=True)  # focus left (not the hotkey's blur) and came back
    rig.c.handle_snapshot(snap())
    rig.ac.trigger()
    rig.c.handle_focus_lost("timeout")
    rig.c.handle_snapshot(snap())
    rig.finish(["tomorrow."])
    assert not rig.ac.visible
    # A blur followed by the same place is the hotkey's own grab: see the tests below.


def test_gated_field_never_completes(tmp_path):
    rig = Rig(tmp_path, choice="api")
    rig.c.handle_snapshot(snap(gated="password", text=""))
    rig.ac.trigger()
    rig.clock.advance(5_000)
    rig.c.handle_snapshot(snap())
    rig.c.handle_snapshot(snap(gated="denylist", text=""))
    rig.ac.trigger()
    rig.clock.advance(5_000)
    rig.ac.on_snapshot(snap(text=TEXT + " now", gated="password"))  # never sent by the controller
    rig.ac.trigger()
    rig.clock.advance(5_000)
    assert not rig.completes() and not rig.ac.visible and not rig.ac_grabs()


def test_a_failing_ungrab_still_hides_the_ghost(rig):
    rig.show()

    def boom(name):
        raise RuntimeError("no display")

    rig.hotkeys.ungrab = boom
    rig.ac.dismiss()
    assert not rig.ac.visible and rig.ghost.shown is None


def test_a_failing_ghost_does_not_leave_keys_grabbed(rig):
    def boom(*a):
        raise RuntimeError("no display")

    rig.ghost.show = boom
    rig.c.handle_snapshot(snap())
    rig.ac.trigger()
    assert not rig.ac.visible and not rig.ac_grabs()


# ---------------------------------------------------------------- the hotkey's own blur


@pytest.mark.parametrize("finish_first", [False, True])
def test_on_demand_completion_survives_the_blur_its_hotkey_causes(tmp_path, finish_first):
    """The complete hotkey's key grab blurs the app on the press and gives the same field
    back on the release; the completion asked for must still be shown."""
    rig = Rig(tmp_path, held=True)
    rig.c.handle_snapshot(snap())
    rig.ac.trigger()
    if finish_first:  # a fast backend: the ghost is up before the blur arrives
        rig.finish([" tomorrow morning."])
        assert rig.ac.visible
    rig.c.handle_focus_lost("blur")
    rig.clock.advance(300)
    rig.c.handle_snapshot(snap())
    if not finish_first:
        rig.finish([" tomorrow morning."])
    assert rig.ac.visible and rig.ghost.shown.text == " tomorrow morning."
    assert rig.ac_grabs() == {"ac_tab", "ac_word"}
    rig.hotkeys.press("ac_tab")
    assert rig.capture.applies[-1].edit == Edit(14, 14, " tomorrow morning.")


def test_on_demand_completion_is_dropped_when_the_field_stays_away(tmp_path):
    rig = Rig(tmp_path, held=True)
    rig.c.handle_snapshot(snap())
    rig.ac.trigger()
    rig.c.handle_focus_lost("blur")
    rig.clock.advance(2_001)
    rig.finish([" tomorrow morning."])
    rig.c.handle_snapshot(snap())
    assert not rig.ac.visible and rig.ghost.shown is None and not rig.ac_grabs()


def test_on_demand_completion_is_dropped_when_another_place_comes_back(tmp_path):
    rig = Rig(tmp_path, held=True)
    rig.c.handle_snapshot(snap())
    rig.ac.trigger()
    rig.c.handle_focus_lost("blur")
    rig.c.handle_snapshot(snap(TEXT + " more"))
    rig.finish([" tomorrow morning."])
    assert not rig.ac.visible and rig.ghost.shown is None


def test_a_blur_long_after_the_hotkey_still_hides(tmp_path):
    rig = Rig(tmp_path, held=True)
    rig.c.handle_snapshot(snap())
    rig.ac.trigger()
    rig.finish([" tomorrow morning."])
    rig.clock.advance(1_000)
    rig.c.handle_focus_lost("blur")
    assert not rig.ac.visible and not rig.ac_grabs()


@pytest.mark.parametrize("blur_after_typing", [False, True])
def test_accept_word_remainder_survives_the_blur_its_key_grab_causes(rig, blur_after_typing):
    """Ctrl+Right is a key grab too: the app blurs on the press and the same field comes back
    on the release, by then with the word typed in. The rest of the suggestion must come
    back with it."""
    rig.show(text="see you", completion=" tomorrow morning.")
    rig.hotkeys.press("ac_word")
    apply_id = rig.capture.applies[-1].apply_id
    if blur_after_typing:  # the release, the word, then the blur report: any order
        rig.c.handle_apply_result(ApplyResult(apply_id, "verified", ""))
    rig.c.handle_focus_lost("blur")
    rig.clock.advance(300)
    if not blur_after_typing:
        rig.c.handle_apply_result(ApplyResult(apply_id, "verified", ""))
    rig.c.handle_snapshot(snap(text="see you tomorrow", caret=16))
    assert rig.ac.visible and rig.ghost.shown.text == " morning."
    assert rig.ac_grabs() == {"ac_tab", "ac_word"}


def test_accept_word_remainder_is_dropped_when_the_field_stays_away(rig):
    rig.show(text="see you", completion=" tomorrow morning.")
    rig.hotkeys.press("ac_word")
    rig.c.handle_focus_lost("blur")
    rig.clock.advance(2_001)
    rig.c.handle_snapshot(snap(text="see you tomorrow", caret=16))
    assert not rig.ac.visible and not rig.ac_grabs()


def test_a_blur_long_after_accept_word_drops_the_remainder(rig):
    rig.show(text="see you", completion=" tomorrow morning.")
    rig.hotkeys.press("ac_word")
    rig.clock.advance(600)  # not the key grab's own blur: the user went elsewhere
    rig.c.handle_focus_lost("blur")
    rig.c.handle_snapshot(snap(text="see you tomorrow", caret=16))
    assert not rig.ac.visible
