import pytest

from glance.audience.profiles import BUILTIN, load_profiles
from glance.audience.resolver import DEFAULT_RULES, AudienceResolver
from glance.backends.base import Analysis, AnalysisIssue, BackendError, Health
from glance.backends.budget import Budget
from glance.backends.supervisor import BackendSupervisor
from glance.config import BackendCfg
from glance.daemon.ai import AiCoordinator
from glance.daemon.cache import LruCache
from glance.daemon.controller import Controller
from glance.grammar.filters import IssueFilter
from glance.grammar.store import IgnoredRules, PersonalDictionary
from glance.protocol import FieldSnapshot, Rect
from glance.ui.chip_model import ChipState, ChipView
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
DM = "Sam Lee (DM) - Acme - Slack"
TEXT = "send me teh report now, i need it before the meeting starts."
SECRET = "sk-ant-api03-AbCdEf0123456789xyzQRSTUV"
SHIELDED = f"ask @sam to rotate {SECRET} then send teh report to the channel"
TEH = AnalysisIssue("teh", 1, "Spelling", "the", "grammar")
BLUNT = Analysis("blunt", True, "curt for a channel", (TEH,))
CALM = Analysis("friendly", False, None, ())
TYPO = {"teh": ("Typo", ("the",))}


def snap(text=TEXT, field_id="f1", title=CHANNEL, wm_class="Slack", gated=None):
    return FieldSnapshot(
        app="Slack", wm_class=wm_class, title=title, url=None, role="text", text=text,
        caret=len(text), caret_rect=Rect(100, 200, 1, 18), prev_char_rect=None,
        field_rect=Rect(90, 190, 400, 300), window_rect=Rect(50, 50, 800, 600),
        field_id=field_id, capture_confidence=1.0, seq=1, pid=100, gated=gated)


class NoOverrides:
    def get(self, key):
        return None


class Rig:
    def __init__(self, tmp_path, analysis=BLUNT, choice="claude", patterns=None,
                 profiles=BUILTIN, delay_ms=0, health=None, **ai_kw):
        self.clock = FakeClock()
        self.cfg = Config(backend=BackendCfg(choice=choice))
        self.battery = False
        self.backend = FakeBackend("claude", analysis, delay_ms=delay_ms)
        if health is not None:
            self.backend.health_value = health
        self.harper = FakeHarper(patterns or {})
        self.overlay = FakeOverlay()
        self.notifier = FakeNotifier()
        self.tray = FakeTray()
        self.bg = FakeBg(self.clock, self.backend)
        self.c = Controller(
            self.clock, lambda: self.cfg, self.harper,
            IssueFilter(PersonalDictionary(tmp_path / "dictionary.txt"),
                        IgnoredRules(tmp_path / "ignored.json"), []),
            self.overlay, FakeCapture(), self.notifier, self.tray, self.bg)
        self.budget = Budget(self.clock, lambda: self.cfg.backend, tmp_path / "usage.json")
        self.statuses: list[str] = []
        self.sup = BackendSupervisor(
            {"claude": self.backend}, lambda: self.cfg.backend, self.budget, self.clock,
            lambda: self.battery, self._on_status)
        self.cache = LruCache()
        self.ai = AiCoordinator(
            self.c, self.sup, AudienceResolver(profiles, DEFAULT_RULES, NoOverrides()),
            self.cache, self.clock, self.bg, **ai_kw)
        self.ai.attach()

    def _on_status(self, status):  # as the daemon wires it: hop to the main loop
        self.statuses.append(status)
        self.clock.post(lambda: self.ai.on_ai_status(status))

    def analyzed(self, s=None, **kw):
        """Send a snapshot and wait out the analyze idle time."""
        s = s or snap(**kw)
        self.c.handle_snapshot(s)
        self.clock.advance(1200)
        return s

    def use_up_budget(self):
        while self.budget.try_spend():
            pass

    def llm(self):
        return [i for i in self.c.issues if i.source == "llm"]


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


# ---------------------------------------------------------------- the analyze path


def test_analyze_after_idle_sets_tone_chip(rig):
    assert len(TEXT) == 60
    rig.c.handle_snapshot(snap())
    rig.clock.advance(1199)
    assert rig.backend.calls == []
    rig.clock.advance(1)
    assert rig.overlay.chip.state is ChipState.TONE
    assert rig.overlay.chip.label == "Blunt · 1"
    assert rig.ai.analysis() == BLUNT
    call = rig.backend.calls[-1]
    assert call.kind == "analyze" and call.text == TEXT
    assert (call.ctx.profile, call.ctx.app_label) == ("team_channel", "Slack")
    assert call.ctx.target_tone == BUILTIN["team_channel"].target_tone
    assert call.ctx.mixed_language is False


def test_llm_issue_joins_the_issue_list_as_unsafe(rig):
    rig.analyzed()
    (issue,) = rig.c.issues
    start = TEXT.index("teh")
    assert (issue.source, issue.rule, issue.start, issue.end) == ("llm", "llm:grammar", start,
                                                                  start + 3)
    assert issue.id == f"llm:llm:grammar:{start}:{start + 3}"
    assert issue.replacements == ("the",) and issue.safe is False
    rig.c.fix_all_safe()  # an LLM suggestion is never applied in bulk
    assert rig.c._capture.applies == []


def test_matching_tone_keeps_the_plain_chip(tmp_path):
    rig = Rig(tmp_path, analysis=CALM)
    rig.analyzed()
    assert rig.overlay.chip.state is ChipState.IDLE
    assert rig.ai.analysis() == CALM


def test_spinner_while_analyzing(tmp_path):
    rig = Rig(tmp_path, delay_ms=500)
    rig.c.handle_snapshot(snap())
    rig.clock.advance(1199)
    assert rig.overlay.chip.state is ChipState.IDLE
    rig.clock.advance(1)
    assert rig.overlay.chip.state is ChipState.SPINNER
    assert rig.ai.analysis() is None
    rig.clock.advance(450)
    assert rig.overlay.chip.state is ChipState.SPINNER
    rig.clock.advance(100)
    assert rig.overlay.chip.state is ChipState.TONE


def test_caret_move_during_analysis_keeps_the_result(tmp_path):
    rig = Rig(tmp_path, delay_ms=500)
    rig.analyzed()
    moved = FieldSnapshot(**{**snap().__dict__, "caret": 3, "seq": 2})
    rig.c.handle_snapshot(moved)  # same text: nothing is cancelled
    assert rig.backend.cancelled == 0
    rig.clock.advance(600)
    assert rig.overlay.chip.state is ChipState.TONE and rig.ai.analysis() == BLUNT


@pytest.mark.parametrize("analysis, patterns, state", [
    (CALM, TYPO, ChipState.FIXES),
    (CALM, {}, ChipState.IDLE),
])
def test_spinner_gives_way_to_fixes_or_idle(tmp_path, analysis, patterns, state):
    rig = Rig(tmp_path, analysis=analysis, patterns=patterns, delay_ms=300)
    rig.analyzed()
    assert rig.overlay.chip.state is ChipState.SPINNER
    rig.clock.advance(300)
    assert rig.overlay.chip.state is state


def test_mixed_language_reaches_the_backend(tmp_path):
    seen = []
    rig = Rig(tmp_path, language=lambda text: seen.append(text) or True)
    rig.analyzed()
    assert seen == [TEXT]  # the detector judges the user's text, not the shielded one
    assert rig.backend.calls[-1].ctx.mixed_language is True


# ---------------------------------------------------------------- shielding


def test_backend_sees_shielded_text_only(rig):
    rig.analyzed(text=SHIELDED)
    sent = rig.backend.calls[-1].text
    assert "[[GLANCE_S1]]" in sent and "[[GLANCE_E1]]" in sent
    assert SECRET not in sent and "sk-ant" not in sent and "@sam" not in sent
    assert sent == "ask [[GLANCE_E1]] to rotate [[GLANCE_S1]] then send teh report to the channel"


def test_llm_issue_offsets_map_back_to_original(rig):
    rig.analyzed(text=SHIELDED)
    (issue,) = rig.llm()
    assert SHIELDED[issue.start:issue.end] == "teh"
    assert issue.start == SHIELDED.index("teh") > SHIELDED.index(SECRET)
    assert issue.id.endswith(f":{issue.start}:{issue.end}")


def test_issue_touching_a_placeholder_or_inventing_one_is_dropped(tmp_path):
    analysis = Analysis("blunt", False, None, (
        AnalysisIssue("rotate [[GLANCE_S1]]", 1, "Unclear", "rotate it", "clarity"),
        AnalysisIssue("teh", 1, "Spelling", "[[GLANCE_E1]]", "grammar"),
        AnalysisIssue("not in the text", 1, "Nope", "x", "clarity"),
        AnalysisIssue("channel", 1, "Vague", "team", "clarity"),
        AnalysisIssue("channel", 1, "Vague", "team", "clarity"),  # the same finding twice
    ))
    rig = Rig(tmp_path, analysis=analysis)
    rig.analyzed(text=SHIELDED)
    (issue,) = rig.llm()
    assert SHIELDED[issue.start:issue.end] == "channel"


def test_placeholders_in_model_prose_are_resolved_for_display(tmp_path):
    analysis = Analysis("blunt", True, "curt towards [[GLANCE_E1]] about [[GLANCE_S1]]", (
        AnalysisIssue("teh", 1, "Typo near [[GLANCE_E1]]", "the", "grammar"),))
    rig = Rig(tmp_path, analysis=analysis)
    rig.analyzed(text=SHIELDED)
    assert rig.ai.analysis().reason == "curt towards @sam about [hidden]"
    assert rig.llm()[0].message == "Typo near @sam"
    rig.c.open_panel()
    assert SECRET not in (rig.overlay.panel.tone_reason or "")


# ---------------------------------------------------------------- staleness and cancelling


def test_stale_analysis_dropped_and_cancelled(tmp_path):
    rig = Rig(tmp_path, delay_ms=500)
    rig.analyzed()
    assert rig.backend.cancelled == 0
    rig.c.handle_snapshot(snap(text=TEXT + " x"))  # typed during the analysis
    assert rig.backend.cancelled == 1
    rig.clock.advance(500)  # the late result arrives; the chip is up again (idle 400 ms)
    assert rig.ai.analysis() is None and rig.c.issues == ()
    assert rig.overlay.chip.state is ChipState.IDLE
    rig.c.handle_snapshot(snap())  # back to the analysed text: nothing was kept for it
    rig.clock.advance(400)
    assert rig.ai.analysis() is None


def test_typing_without_an_analysis_in_flight_cancels_nothing(rig):
    rig.analyzed()
    rig.c.handle_snapshot(snap(text=TEXT + " x"))
    assert rig.backend.cancelled == 0


@pytest.mark.parametrize("leave", ["focus", "gated", "pause", "capture"])
def test_leaving_the_text_cancels_and_drops_the_analysis(tmp_path, leave):
    rig = Rig(tmp_path, delay_ms=500)
    rig.analyzed()
    if leave == "focus":
        rig.c.handle_focus_lost("blur")
    elif leave == "gated":
        rig.c.handle_snapshot(snap(text="", gated="password"))
    elif leave == "pause":
        rig.c.toggle_pause()
    else:
        rig.c.handle_capture_disconnected()
    assert rig.backend.cancelled == 1
    rig.clock.advance(500)
    assert rig.ai.analysis() is None
    assert rig.cache.get(rig.ai._key(snap(), rig.ai._resolve(snap()))) is None
    assert len(rig.backend.calls) == 1  # nothing is sent again on its own


def test_cancelled_call_is_silent(tmp_path):
    rig = Rig(tmp_path)
    rig.backend.script(BackendError("cancelled"))
    rig.analyzed()
    assert rig.overlay.chip.state is ChipState.IDLE
    assert rig.notifier.calls == [] and rig.ai.analysis() is None


# ---------------------------------------------------------------- cache


def test_cache_hit_makes_no_second_call(rig):
    rig.analyzed()
    rig.c.handle_focus_lost("blur")
    assert rig.ai.analysis() is None and rig.overlay.chip is None
    rig.analyzed()  # focus back on the same text
    assert len(rig.backend.calls) == 1
    assert rig.overlay.chip.state is ChipState.TONE and rig.ai.analysis() == BLUNT
    assert len(rig.llm()) == 1


def test_cache_hit_when_analysis_falls_due_for_known_text(rig):
    rig.analyzed()
    rig.analyzed(field_id="f2")  # another field, same text and audience: analysis is due
    assert len(rig.backend.calls) == 1
    assert rig.overlay.chip.state is ChipState.TONE


def test_cache_is_per_audience(rig):
    rig.analyzed()
    rig.analyzed(field_id="f2", title=DM)
    assert len(rig.backend.calls) == 2
    assert rig.backend.calls[-1].ctx.profile == "dm"


def test_cache_keys_hold_no_text(rig):
    rig.analyzed()
    assert all(len(k) == 64 and "report" not in k for k in rig.cache._items)


def test_edited_text_loses_the_tone_until_analysed_again(rig):
    rig.analyzed()
    rig.c.handle_snapshot(snap(text=TEXT + "!"))
    rig.clock.advance(400)
    assert rig.ai.analysis() is None and rig.llm() == []
    assert rig.overlay.chip.state is ChipState.IDLE


# ---------------------------------------------------------------- when AI may not run


@pytest.mark.parametrize("why", ["none", "signed_out", "limit", "battery"])
def test_no_auto_analyze_when_not_allowed(tmp_path, why):
    rig = Rig(tmp_path, choice=None if why == "none" else "claude",
              health=Health(True, False, "signed out") if why == "signed_out" else None)
    if why == "limit":
        rig.use_up_budget()
    rig.battery = why == "battery"
    states = []
    show = rig.overlay.show_chip
    rig.overlay.show_chip = lambda view, x, y: (states.append(view.state), show(view, x, y))
    rig.analyzed()
    assert rig.backend.calls == []
    assert ChipState.SPINNER not in states
    assert rig.ai.analysis() is None


def test_chip_says_connect_or_limit_but_keeps_fix_count(tmp_path):
    rig = Rig(tmp_path, choice=None, patterns=TYPO)
    rig.analyzed(text="teh quick brown fox and teh lazy dog went home")
    assert rig.overlay.chip == ChipView(ChipState.FIXES, "2", "none")
    rig.analyzed(text="the quick brown fox and the lazy dog went home")
    assert rig.overlay.chip == ChipView(ChipState.NO_AI, "Connect an AI", None)
    assert rig.tray.state == "no_ai"

    rig = Rig(tmp_path / "limit", patterns=TYPO)
    rig.use_up_budget()
    rig.analyzed(text="the quick brown fox and the lazy dog went home")
    assert rig.overlay.chip == ChipView(ChipState.AI_LIMIT, "AI paused (limit)", None)
    rig.analyzed(text="teh quick brown fox and teh lazy dog went home")
    assert rig.overlay.chip == ChipView(ChipState.FIXES, "2", "limit")


def test_no_ai_is_known_before_any_analysis_is_due(tmp_path):
    rig = Rig(tmp_path, choice=None)
    rig.c.handle_snapshot(snap(text="short but enough"))  # under min_chars_analyze
    rig.clock.advance(400)
    assert rig.overlay.chip == ChipView(ChipState.NO_AI, "Connect an AI", None)


def test_limit_reached_by_the_call_itself_shows_on_the_chip(tmp_path):
    rig = Rig(tmp_path, analysis=CALM)
    rig.cfg = Config(backend=BackendCfg(choice="claude", per_minute=1))
    rig.analyzed()
    assert len(rig.backend.calls) == 1 and rig.statuses == ["limit"]
    assert rig.overlay.chip == ChipView(ChipState.AI_LIMIT, "AI paused (limit)", None)
    rig.clock.advance(61_000)  # the minute passes; the next analysis finds AI available
    rig.analyzed(text="a different message that is long enough to analyse")
    assert len(rig.backend.calls) == 2


def test_supervisor_status_change_redraws(rig):
    rig.analyzed(text="short but enough")  # under min_chars_analyze
    assert rig.overlay.chip.state is ChipState.IDLE
    rig.ai.on_ai_status("none")
    assert rig.overlay.chip.state is ChipState.NO_AI
    assert rig.c.status()["ai"] == "none"


def test_profile_check_false_skips(tmp_path):
    profiles = load_profiles({"profile": [{"name": "team_channel", "check": False}]})
    rig = Rig(tmp_path, profiles=profiles, patterns=TYPO)
    rig.analyzed()
    assert rig.backend.calls == []
    assert rig.ai.analysis() is None
    assert rig.overlay.chip == ChipView(ChipState.FIXES, "1", None)  # local grammar goes on
    rig.analyzed(field_id="f2", title=DM)  # a profile that is checked still analyzes
    assert len(rig.backend.calls) == 1


# ---------------------------------------------------------------- merging with local grammar


def test_harper_wins_over_overlapping_llm_issue(tmp_path):
    analysis = Analysis("blunt", True, "curt", (
        AnalysisIssue("me teh report", 1, "Reword", "me the report", "clarity"),
        AnalysisIssue("meeting", 1, "Vague", "standup", "clarity"),
    ))
    rig = Rig(tmp_path, analysis=analysis, patterns=TYPO)
    rig.analyzed()
    assert [(i.source, TEXT[i.start:i.end]) for i in rig.c.issues] == [
        ("harper", "teh"), ("llm", "meeting")]
    assert rig.overlay.chip.label == "Blunt · 2"


def test_profile_ignore_rules_reach_the_filter(tmp_path):
    rig = Rig(tmp_path, analysis=CALM, patterns={"send": ("SentenceCapitalization", ("Send",))})
    rig.analyzed(title=DM)
    assert rig.c.issues == ()  # the dm profile does not nag about lowercase
    rig.analyzed(field_id="f2")
    assert [i.rule for i in rig.c.issues] == ["SentenceCapitalization"]


# ---------------------------------------------------------------- failures


@pytest.mark.parametrize("kind", ["timeout", "bad_output", "unavailable", "rate_limit"])
def test_backend_error_is_silent_and_clears_spinner(tmp_path, kind, capsys):
    rig = Rig(tmp_path, delay_ms=200)
    rig.backend.script(BackendError(kind, "boom"))
    rig.analyzed()
    assert rig.overlay.chip.state is ChipState.SPINNER
    rig.clock.advance(200)
    assert rig.overlay.chip.state is ChipState.IDLE
    assert rig.notifier.calls == []
    assert rig.ai.analysis() is None
    assert rig.c.status()["ai"] == "ok"
    assert f"backend_fail backend=claude kind={kind}" in capsys.readouterr().err


def test_sign_out_during_analysis_turns_the_chip_to_connect(tmp_path):
    rig = Rig(tmp_path)
    rig.backend.script(BackendError("auth"))
    rig.analyzed()
    assert rig.overlay.chip == ChipView(ChipState.NO_AI, "Connect an AI", None)
    assert rig.notifier.calls == []


def test_unexpected_failure_in_the_job_clears_the_spinner(tmp_path, capsys):
    def broken(_text):
        raise RuntimeError(TEXT)

    rig = Rig(tmp_path, language=broken)
    rig.analyzed()
    assert rig.overlay.chip.state is ChipState.IDLE
    err = capsys.readouterr().err
    assert "analyze_error kind=RuntimeError" in err and "report" not in err


def test_no_text_in_logs_during_analyze(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("GLANCE_STRICT_LOG", "1")  # an unsafe log value would raise
    rig = Rig(tmp_path, delay_ms=100)
    rig.backend.script(BackendError("bad_output", SHIELDED))
    rig.analyzed(text=SHIELDED)
    rig.clock.advance(100)
    rig.analyzed(text=SHIELDED + " and more words here")
    rig.c.handle_snapshot(snap(text=SHIELDED + " typed during the call"))
    rig.clock.advance(2000)
    rig.c.status()
    out = capsys.readouterr()
    assert len(rig.backend.calls) == 3
    for fragment in ("rotate", "report", "channel", "@sam", "sk-ant", "GLANCE_"):
        assert fragment not in out.err and fragment not in out.out


# ---------------------------------------------------------------- the coordinator's surface


def test_status_entries(rig):
    assert rig.c.status()["ai"] == "ok"
    assert (rig.c.status()["backend"], rig.c.status()["profile"]) == ("claude", "")
    rig.analyzed()
    status = rig.c.status()
    assert (status["ai"], status["backend"], status["profile"]) == ("ok", "claude",
                                                                    "team_channel")


def test_status_without_a_backend(tmp_path):
    rig = Rig(tmp_path, choice=None)
    assert (rig.c.status()["ai"], rig.c.status()["backend"]) == ("none", "")


def test_resolved_follows_the_field(rig):
    assert rig.ai.resolved() is None
    rig.c.handle_snapshot(snap())
    assert rig.ai.resolved().profile.name == "team_channel"
    rig.c.handle_snapshot(snap(field_id="f2", title=DM))
    assert rig.ai.resolved().profile.name == "dm"
    rig.c.handle_snapshot(snap(text="", gated="password"))
    assert rig.ai.resolved() is None
    rig.c.handle_snapshot(snap())
    rig.c.handle_focus_lost("blur")
    assert rig.ai.resolved() is None


def test_panel_shows_tone_and_reason(rig):
    rig.analyzed()
    rig.c.open_panel()
    assert (rig.overlay.panel.tone, rig.overlay.panel.tone_reason) == ("Blunt",
                                                                       "curt for a channel")
    assert len(rig.overlay.panel.issues) == 1


def test_panel_without_analysis_has_no_tone(tmp_path):
    rig = Rig(tmp_path, choice=None, patterns=TYPO)
    rig.analyzed()
    rig.c.open_panel()
    assert rig.overlay.panel.tone is None and len(rig.overlay.panel.issues) == 1


def test_profile_override_applies_to_this_context_and_reanalyzes(rig):
    rig.analyzed()
    rig.ai.set_profile_override("email")
    assert rig.ai.resolved().profile.name == "email"
    assert rig.ai.resolved().source == "override"
    assert rig.backend.calls[-1].ctx.profile == "email" and len(rig.backend.calls) == 2
    rig.c.handle_snapshot(snap(field_id="f2", title=DM))  # another conversation
    assert rig.ai.resolved().profile.name == "dm"
    rig.c.handle_snapshot(snap())
    assert rig.ai.resolved().profile.name == "email"
    rig.ai.set_profile_override(None)
    assert rig.ai.resolved().profile.name == "team_channel"
    assert len(rig.backend.calls) == 2  # the first analysis is still cached


def test_profile_override_rejects_unknown_names_and_needs_a_field(rig):
    rig.ai.set_profile_override("email")  # no field: nothing to override
    rig.c.handle_snapshot(snap())
    assert rig.ai.resolved().profile.name == "team_channel"
    with pytest.raises(ValueError):
        rig.ai.set_profile_override("no_such_profile")


def test_attach_appends_to_hooks_and_chains_on_analyze_due(tmp_path):
    rig = Rig(tmp_path)
    c = rig.c
    due = []
    assert len(c.extra_chip_inputs) == len(c.extra_issues) == len(c.extra_ignored) == 1
    assert len(c.extra_panel) == len(c.extra_status) == 1

    rig2 = Rig(tmp_path / "second")
    c2 = Controller(rig2.clock, lambda: rig2.cfg, rig2.harper, rig2.c._filter, rig2.overlay,
                    FakeCapture(), rig2.notifier, rig2.tray, rig2.bg)
    c2.on_analyze_due = lambda s, gen: due.append(gen)
    c2.extra_chip_inputs.append(lambda s: {"guard": None})
    AiCoordinator(c2, rig2.sup, AudienceResolver(BUILTIN, DEFAULT_RULES, NoOverrides()),
                  LruCache(), rig2.clock, rig2.bg).attach()
    assert len(c2.extra_chip_inputs) == 2
    c2.handle_snapshot(snap())
    rig2.clock.advance(1200)
    assert due == [c2.generation] and len(rig2.backend.calls) == 1


# ---------------------------------------------------------------- the strip


def test_strip_uses_the_analysis_replacements_without_another_call(rig):
    rig.analyzed()
    calls = len(rig.backend.calls)
    rig.c.toggle_strip()  # the status icon's click opens it
    strip = rig.overlay.strip
    assert strip.text == "send me the report now, i need it before the meeting starts."
    assert [s.text for s in strip.segments if s.changed] == ["the"] and strip.note is None
    rig.clock.advance(60_000)
    assert len(rig.backend.calls) == calls  # the strip asks the AI for nothing


def test_strip_says_the_tone_is_off_when_there_is_nothing_to_fix(tmp_path):
    rig = Rig(tmp_path, analysis=Analysis("blunt", True, "curt", ()))
    rig.analyzed(snap(text="send me the report now, I need it before the meeting starts."))
    assert rig.overlay.strip is None  # not until the status icon is clicked
    assert rig.overlay.status.badge == "•"
    rig.c.toggle_strip()
    strip = rig.overlay.strip
    assert not strip.can_use
    assert strip.note == "Reads blunt for team channel — ⋯ for tone options"


def test_no_strip_when_the_tone_fits_and_nothing_needs_fixing(tmp_path):
    rig = Rig(tmp_path, analysis=CALM)
    rig.analyzed(snap(text="send me the report now, I need it before the meeting starts."))
    assert rig.overlay.strip is None
