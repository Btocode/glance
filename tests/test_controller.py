import json
from dataclasses import replace

import pytest

from glance.config import Config, UiCfg
from glance.daemon.controller import APPLY_TIMEOUT_MS, FIX_INDIVIDUALLY, Controller
from glance.grammar.filters import IssueFilter
from glance.grammar.issues import Issue, merge_edits
from glance.grammar.store import IgnoredRules, PersonalDictionary
from glance.protocol import ApplyResult, Edit, FieldSnapshot, Rect, text_sha256
from glance.ui.chip_model import ChipState
from glance.ui.panel_model import IssueRow, PanelView, ToneChip, build_panel
from tests.fakes import (
    FakeCapture,
    FakeClock,
    FakeHarper,
    FakeNotifier,
    FakeOverlay,
    FakeTray,
)

TEH = "Teh quick brown fox jumps"
MIXED = "Teh quick brwon fox teh end of it"  # safe 0-3, spelling 10-15, safe 20-23
MENTION = "Teh note for @sam about teh plan"
PATTERNS = {
    "Teh": ("Typo", ("The",)),
    "teh": ("Typo", ("the",)),
    "brwon": ("SpellCheck", ("brown", "brawn")),
    "Smiht": ("SpellCheck", ("Smith", "Smit")),  # a capitalised word mid-sentence: a name?
}


CARET = Rect(100, 200, 1, 18)
AUTO = Config(ui=UiCfg(suggestions="auto"))


def snap(text=TEH, field_id="f1", gated=None, wm_class="gedit", seq=1, caret_rect=CARET):
    return FieldSnapshot(
        app="gedit", wm_class=wm_class, title="notes", url=None, role="text", text=text,
        caret=len(text), caret_rect=caret_rect, prev_char_rect=None,
        field_rect=Rect(90, 190, 400, 300), window_rect=Rect(50, 50, 800, 600),
        field_id=field_id, capture_confidence=1.0, seq=seq, pid=100, gated=gated)


class Rig:
    def __init__(self, tmp_path, deferred=False):
        self.clock = FakeClock()
        self.cfg = Config()
        self.harper = FakeHarper(PATTERNS)
        self.overlay = FakeOverlay()
        self.capture = FakeCapture()
        self.notifier = FakeNotifier()
        self.tray = FakeTray()
        self.dictionary = PersonalDictionary(tmp_path / "dictionary.txt")
        self.ignored = IgnoredRules(tmp_path / "ignored.json")
        self.bg: list = []  # (work, done) pairs waiting when `deferred`
        self.deferred = deferred
        self.c = Controller(
            self.clock, lambda: self.cfg, self.harper,
            IssueFilter(self.dictionary, self.ignored, []), self.overlay, self.capture,
            self.notifier, self.tray, self.run_bg)

    def run_bg(self, work, done):
        if self.deferred:
            self.bg.append((work, done))
        else:
            done(work())

    def deliver(self, index=0):
        work, done = self.bg.pop(index)
        done(work())

    def settle(self, s=None, **kw):
        """Send a snapshot and wait out the idle time: the chip is up afterwards."""
        s = s or snap(**kw)
        self.c.handle_snapshot(s)
        self.clock.advance(400)
        return s

    def issue(self, needle):
        text = self.c.current.text
        return next(i for i in self.c.issues if text[i.start:i.end] == needle)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


# ---------------------------------------------------------------- panel model


def test_build_panel_rows_and_excerpt():
    text = "x" * 30 + " Teh " + "y" * 30
    issues = [Issue("harper:Typo:31:34", "harper", "Typo", "Did you mean The?", 31, 34,
                    ("The",), True)]
    view = build_panel(text, issues)
    assert view.issues == (IssueRow("harper:Typo:31:34", "x" * 19 + " Teh " + "y" * 19,
                                    "Did you mean The?", ("The",), "Typo", "Teh"),)
    assert view.can_fix_all is True
    assert (view.tone, view.tone_reason, view.tone_chips) == (None, None, ())
    assert (view.banner, view.copy_offer) == (None, None)


def test_build_panel_excerpt_clamped_at_text_edges_and_no_safe_issue():
    issues = [Issue("harper:SpellCheck:0:3", "harper", "SpellCheck", "m", 0, 3,
                    ("The", "Ten"), False)]
    view = build_panel("Teh end", issues, banner="b", copy_offer="c")
    assert view.issues[0].excerpt == "Teh end"
    assert view.can_fix_all is False
    assert (view.banner, view.copy_offer) == ("b", "c")


def test_panel_view_defaults_and_tone_chip():
    assert PanelView() == PanelView(None, None, (), (), None, False, None)
    assert ToneChip("shorter", "Shorter", 3).hotkey == 3


# ---------------------------------------------------------------- chip


def test_local_issue_shows_fixes_chip_after_settle(rig):
    rig.c.handle_snapshot(snap())
    assert rig.overlay.chip is None
    rig.clock.advance(400)
    assert rig.overlay.chip.state is ChipState.FIXES
    assert rig.overlay.chip.label == "1"
    assert rig.overlay.chip_xy == (105, 222)  # caret (100,200,1,18) + 4 px
    assert rig.tray.state == "active"


def test_chip_hidden_while_typing(rig):
    rig.settle()
    assert rig.overlay.chip is not None
    rig.c.handle_snapshot(snap(TEH + " over", seq=2))
    assert rig.overlay.chip is None
    rig.clock.advance(399)
    assert rig.overlay.chip is None
    rig.clock.advance(1)
    assert rig.overlay.chip.state is ChipState.FIXES


def test_chip_placed_from_latest_snapshot_not_the_scheduler_argument(rig):
    # Same text, new caret, while the settle timer is pending: the scheduler drops the
    # second snapshot and later calls back with the first one.
    rig.c.handle_snapshot(snap())
    rig.clock.advance(100)
    rig.c.handle_snapshot(snap(seq=2, caret_rect=Rect(300, 400, 1, 18)))
    rig.clock.advance(300)
    assert rig.overlay.chip_xy == (305, 422)


def test_caret_move_keeps_chip_until_settled_then_moves_it(rig):
    rig.settle()
    rig.c.handle_snapshot(snap(seq=2, caret_rect=Rect(300, 400, 1, 18)))
    assert rig.overlay.chip_xy == (105, 222)
    rig.clock.advance(50)
    assert rig.overlay.chip_xy == (305, 422)
    assert len(rig.harper.calls) == 1  # same text is not checked again


def test_chip_hidden_when_no_anchor_is_usable(rig):
    rig.settle(replace(snap(), caret_rect=Rect(0, 0, 0, 0), field_rect=None, window_rect=None))
    assert rig.overlay.chip is None
    assert rig.c.status()["local_issues"] == 1


def test_short_text_shows_no_chip(rig):
    rig.settle(text="Teh quick")
    assert rig.overlay.chip is None


# ---------------------------------------------------------------- grammar results


def test_stale_harper_result_dropped(tmp_path):
    rig = Rig(tmp_path, deferred=True)
    rig.c.handle_snapshot(snap(TEH))
    rig.clock.advance(80)
    second = "The quick brown fox jumps over teh dog"
    rig.c.handle_snapshot(snap(second, seq=2))
    rig.clock.advance(250)  # inside a burst the local check waits for a 250 ms quiet
    assert len(rig.bg) == 2
    rig.deliver()  # generation 1 result, after snapshot 2
    assert rig.c.issues == ()
    rig.deliver()
    assert [(i.start, i.end) for i in rig.c.issues] == [(31, 34)]
    assert second[31:34] == "teh"


def test_harper_result_after_focus_lost_dropped(tmp_path):
    rig = Rig(tmp_path, deferred=True)
    rig.c.handle_snapshot(snap())
    rig.clock.advance(80)
    rig.c.handle_focus_lost("blur")
    rig.deliver()
    assert rig.c.issues == () and rig.c.current is None


def test_gated_snapshot_never_reaches_harper_and_closes_panel(rig):
    rig.settle()
    rig.c.open_panel()
    assert rig.overlay.panel is not None
    n_before = len(rig.harper.calls)
    rig.c.handle_snapshot(snap(gated="password", text="", seq=2))
    rig.clock.advance(2000)
    assert len(rig.harper.calls) == n_before
    assert rig.overlay.panel is None
    assert rig.overlay.chip.state is ChipState.PRIVATE
    assert rig.tray.state == "private"
    assert rig.c.issues == ()
    rig.c.open_panel()
    assert rig.overlay.panel is None


def test_harper_dead_shows_degraded_not_crash(rig):
    rig.harper.alive = False
    rig.settle()
    assert rig.tray.state == "degraded"
    assert rig.c.status()["harper_alive"] is False
    assert rig.harper.calls == []
    rig.harper.alive = True
    rig.settle(text=TEH + " again", seq=2)
    assert rig.tray.state == "active" and rig.c.status()["local_issues"] == 1


# ---------------------------------------------------------------- panel


def test_open_panel_lists_issues_below_the_chip(rig):
    rig.settle(text=MIXED)
    rig.c.open_panel()
    view = rig.overlay.panel
    assert [r.rule for r in view.issues] == ["Typo", "SpellCheck", "Typo"]
    assert view.issues[1].replacements == ("brown", "brawn")
    assert view.can_fix_all is True
    assert rig.overlay.panel_xy == (105, 222 + 22 + 4)


def test_open_panel_without_field_does_nothing(rig):
    rig.c.open_panel()
    assert rig.overlay.panel is None


def test_panel_closes_when_the_field_changes(rig):
    rig.settle()
    rig.c.open_panel()
    rig.c.handle_snapshot(snap(field_id="f2", seq=2))
    assert rig.overlay.panel is None


def test_snooze_hides_chip_until_field_changes(rig):
    rig.cfg = AUTO  # the strip shows by itself
    rig.settle()
    rig.c.open_panel()
    rig.c.close_panel(snooze=True)
    assert rig.overlay.panel is None and rig.overlay.chip is None
    rig.settle(text=TEH + " more", seq=2)  # same field: still snoozed
    assert rig.overlay.chip is None
    rig.settle(field_id="f2", seq=3)
    assert rig.overlay.chip.state is ChipState.FIXES


def test_close_panel_without_snooze_keeps_chip(rig):
    rig.settle()
    rig.c.open_panel()
    rig.c.close_panel()
    assert rig.overlay.panel is None and rig.overlay.chip.state is ChipState.FIXES


# ---------------------------------------------------------------- apply


def test_accept_issue_sends_apply_with_snapshot_hash(rig):
    s = rig.settle()
    rig.c.accept_issue(rig.issue("Teh").id)
    r = rig.capture.applies[-1]
    assert (r.edit, r.expected_sha256, r.field_id) == (
        Edit(0, 3, "The"), text_sha256(s.text), s.field_id)
    assert r.method == "auto" and r.apply_id


def test_accept_issue_choice_picks_the_replacement(rig):
    rig.settle(text=MIXED)
    rig.c.accept_issue(rig.issue("brwon").id, choice=1)
    assert rig.capture.applies[-1].edit == Edit(10, 15, "brawn")
    rig.c.handle_apply_result(ApplyResult(rig.capture.applies[-1].apply_id, "stale", ""))
    rig.c.accept_issue(rig.issue("brwon").id, choice=2)  # no such replacement
    rig.c.accept_issue("harper:Nope:0:1")
    assert len(rig.capture.applies) == 1


def test_no_second_apply_while_one_pending(rig):
    rig.settle(text=MIXED)
    rig.c.accept_issue(rig.issue("Teh").id)
    rig.c.accept_issue(rig.issue("teh").id)
    rig.c.fix_all_safe()
    assert rig.c.request_apply(Edit(0, 0, "x")) == ""
    assert len(rig.capture.applies) == 1
    rig.c.handle_apply_result(ApplyResult(rig.capture.applies[0].apply_id, "stale", ""))
    rig.c.accept_issue(rig.issue("teh").id)
    assert len(rig.capture.applies) == 2
    assert rig.capture.applies[1].apply_id != rig.capture.applies[0].apply_id


def test_fix_all_safe_sends_one_merged_edit_and_skips_unsafe(rig):
    s = rig.settle(text=MIXED)
    rig.c.fix_all_safe()
    assert len(rig.capture.applies) == 1
    r = rig.capture.applies[0]
    assert r.edit == merge_edits(MIXED, [Edit(0, 3, "The"), Edit(20, 23, "the")])
    assert r.edit == Edit(0, 23, "The quick brwon fox the")
    assert r.expected_sha256 == text_sha256(s.text)


def test_fix_all_with_nothing_safe_sends_nothing(rig):
    rig.settle(text="The quick brwon fox jumps")
    rig.c.fix_all_safe()
    assert rig.capture.applies == [] and rig.overlay.panel is None


def test_fix_all_refuses_to_span_a_mention(rig):
    rig.settle(text=MENTION)
    assert len(rig.c.issues) == 2
    rig.c.fix_all_safe()
    assert rig.capture.applies == []
    assert rig.overlay.panel.banner.startswith("Fix individually")
    assert rig.overlay.panel.banner == FIX_INDIVIDUALLY
    assert FIX_INDIVIDUALLY == "Fix individually — text between fixes is protected"
    rig.c.accept_issue(rig.issue("Teh").id)  # one at a time still works
    assert rig.capture.applies[-1].edit == Edit(0, 3, "The")


def test_request_apply_returns_id_and_uses_current_hash(rig):
    s = rig.settle()
    aid = rig.c.request_apply(Edit(0, len(TEH), "A fox."), "paste")
    r = rig.capture.applies[-1]
    assert aid and r.apply_id == aid
    assert (r.edit, r.method, r.expected_sha256) == (
        Edit(0, len(TEH), "A fox."), "paste", text_sha256(s.text))


def test_request_apply_accepts_the_hash_of_an_older_snapshot(rig):
    rig.settle()
    rig.c.request_apply(Edit(0, 0, "x"), expected_sha256=text_sha256("earlier"))
    assert rig.capture.applies[-1].expected_sha256 == text_sha256("earlier")


def test_no_apply_without_field_when_gated_paused_or_disconnected(rig):
    assert rig.c.request_apply(Edit(0, 0, "x")) == ""
    rig.settle(gated="password", text="")
    assert rig.c.request_apply(Edit(0, 0, "x")) == ""
    rig.settle(seq=2)
    rig.capture.connected = False
    assert rig.c.request_apply(Edit(0, 0, "x")) == ""
    rig.capture.connected = True
    rig.c.toggle_pause()
    assert rig.c.request_apply(Edit(0, 0, "x")) == ""
    assert rig.capture.applies == []


# ---------------------------------------------------------------- apply results


def _accept(rig, text=TEH):
    rig.settle(text=text)
    rig.c.open_panel()
    rig.c.accept_issue(rig.issue("Teh").id)
    return rig.capture.applies[-1].apply_id


def test_verified_result_drops_issues_until_the_next_snapshot(rig):
    aid = _accept(rig, MIXED)
    rig.c.handle_apply_result(ApplyResult(aid, "verified", ""))
    assert rig.c.issues == () and rig.notifier.calls == []
    fixed = "The quick brwon fox teh end of it"
    rig.settle(text=fixed, seq=2)
    assert [fixed[i.start:i.end] for i in rig.c.issues] == ["brwon", "teh"]
    rig.c.accept_issue(rig.issue("teh").id)  # nothing is pending any more
    assert len(rig.capture.applies) == 2


def test_verified_result_after_the_new_snapshot_keeps_its_issues(rig):
    aid = _accept(rig, MIXED)
    fixed = "The quick brwon fox teh end of it"
    rig.settle(text=fixed, seq=2)
    rig.c.handle_apply_result(ApplyResult(aid, "verified", ""))
    assert [fixed[i.start:i.end] for i in rig.c.issues] == ["brwon", "teh"]


def test_reverted_result_notifies_user(rig):
    aid = _accept(rig)
    rig.c.handle_apply_result(ApplyResult(aid, "reverted", "mismatch"))
    assert rig.notifier.calls == [
        ("Glance undid a change", "The app changed the text differently. Nothing was lost.")]


@pytest.mark.parametrize("status", ["failed", "refused"])
def test_failed_result_offers_copy(rig, status):
    aid = _accept(rig)
    rig.c.handle_apply_result(ApplyResult(aid, status, "…"))
    assert rig.overlay.panel.copy_offer == "The"
    assert len(rig.notifier.calls) == 1
    rig.c.copy_instead()
    assert rig.overlay.clipboard == ["The"]
    assert rig.overlay.panel.copy_offer is None


def test_copy_offer_cleared_when_panel_closes(rig):
    aid = _accept(rig)
    rig.c.handle_apply_result(ApplyResult(aid, "failed", ""))
    rig.c.close_panel()
    rig.c.open_panel()
    assert rig.overlay.panel.copy_offer is None
    rig.c.copy_instead()
    assert rig.overlay.clipboard == []


@pytest.mark.parametrize("status", ["stale", "unverified", "something_new"])
def test_stale_and_unknown_results_are_silent(rig, status):
    aid = _accept(rig)
    rig.c.handle_apply_result(ApplyResult(aid, status, ""))
    assert rig.notifier.calls == [] and rig.overlay.panel.copy_offer is None
    assert len(rig.c.issues) == 1


def test_result_for_an_unknown_apply_is_ignored(rig):
    _accept(rig)
    rig.c.handle_apply_result(ApplyResult("zz", "failed", ""))
    assert rig.notifier.calls == [] and rig.overlay.panel.copy_offer is None
    rig.c.accept_issue(rig.issue("Teh").id)
    assert len(rig.capture.applies) == 1  # the real one is still pending


def test_revert_last_sends_revert_and_stays_quiet_on_success(rig):
    aid = _accept(rig)
    rig.c.handle_apply_result(ApplyResult(aid, "verified", ""))
    rig.c.revert_last()
    assert rig.capture.reverts == 1
    rig.c.handle_apply_result(ApplyResult(aid, "reverted", ""))
    assert rig.notifier.calls == []


def test_revert_last_failure_notifies(rig):
    aid = _accept(rig)
    rig.c.handle_apply_result(ApplyResult(aid, "verified", ""))
    rig.c.revert_last()
    rig.c.handle_apply_result(ApplyResult(aid, "failed", "could not restore"))
    assert [c[0] for c in rig.notifier.calls] == ["Glance could not undo the change"]


def test_apply_without_a_result_times_out(rig):
    aid = _accept(rig, MIXED)
    rig.clock.advance(APPLY_TIMEOUT_MS - 1)
    rig.c.accept_issue(rig.issue("teh").id)
    assert len(rig.capture.applies) == 1 and rig.notifier.calls == []
    rig.clock.advance(1)
    assert rig.overlay.panel.copy_offer == "The"
    assert [c[0] for c in rig.notifier.calls] == ["Glance could not apply the change"]
    rig.c.accept_issue(rig.issue("teh").id)  # applies work again
    second = rig.capture.applies[-1]
    assert len(rig.capture.applies) == 2 and second.edit == Edit(20, 23, "the")
    # The lost result turns up after all: it changes nothing, and the new apply stays pending.
    rig.c.handle_apply_result(ApplyResult(aid, "reverted", ""))
    rig.c.handle_apply_result(ApplyResult(aid, "verified", ""))
    assert len(rig.notifier.calls) == 1 and len(rig.c.issues) == 3
    rig.c.accept_issue(rig.issue("Teh").id)
    assert len(rig.capture.applies) == 2
    rig.clock.advance(APPLY_TIMEOUT_MS - 1)  # the first apply's timer must not end this one
    assert len(rig.notifier.calls) == 1
    rig.c.handle_apply_result(ApplyResult(second.apply_id, "stale", ""))
    rig.clock.advance(APPLY_TIMEOUT_MS)  # a result disarms the timer
    assert len(rig.notifier.calls) == 1 and rig.overlay.panel.copy_offer is None


def test_apply_timeout_disarmed_by_capture_disconnect(rig):
    _accept(rig)
    rig.c.handle_capture_disconnected()
    rig.clock.advance(APPLY_TIMEOUT_MS)
    assert rig.notifier.calls == []


def test_late_result_of_a_timed_out_apply_is_not_taken_for_a_revert_reply(rig):
    first = _accept(rig)
    rig.c.handle_apply_result(ApplyResult(first, "verified", ""))
    rig.settle(text=MIXED, seq=2)
    rig.c.accept_issue(rig.issue("teh").id)
    lost = rig.capture.applies[-1].apply_id
    rig.clock.advance(APPLY_TIMEOUT_MS)
    assert len(rig.notifier.calls) == 1
    rig.c.revert_last()
    rig.c.handle_apply_result(ApplyResult(lost, "failed", ""))  # not the revert's answer
    assert len(rig.notifier.calls) == 1
    rig.c.handle_apply_result(ApplyResult(first, "failed", "could not restore"))
    assert rig.notifier.calls[-1][0] == "Glance could not undo the change"
    rig.c.handle_apply_result(ApplyResult(first, "failed", ""))  # answered already
    assert len(rig.notifier.calls) == 2


def test_revert_sent_while_an_apply_is_pending_reverts_that_apply(rig):
    first = _accept(rig)
    rig.c.handle_apply_result(ApplyResult(first, "verified", ""))
    rig.settle(text=MIXED, seq=2)
    rig.c.accept_issue(rig.issue("teh").id)
    second = rig.capture.applies[-1].apply_id
    rig.c.revert_last()  # capture handles the apply first, so this undoes `second`
    rig.c.handle_apply_result(ApplyResult(second, "verified", ""))
    rig.c.handle_apply_result(ApplyResult(second, "refused", "release modifier keys"))
    assert [c[0] for c in rig.notifier.calls] == ["Glance could not undo the change"]


def test_revert_with_nothing_to_revert_is_silent(rig):
    rig.settle()
    rig.c.revert_last()
    rig.c.handle_apply_result(ApplyResult("", "stale", "nothing to revert"))
    assert rig.notifier.calls == []


def test_failed_apply_offers_no_copy_in_another_field(rig):
    aid = _accept(rig)
    rig.settle(field_id="f2", seq=2)
    rig.c.open_panel()
    rig.c.handle_apply_result(ApplyResult(aid, "failed", ""))
    assert rig.overlay.panel.copy_offer is None
    assert len(rig.notifier.calls) == 1


def test_pending_apply_survives_focus_loss_and_pause_until_its_result(rig):
    aid = _accept(rig)
    rig.c.handle_focus_lost("blur")
    rig.c.toggle_pause()
    rig.c.resume()
    rig.c.handle_apply_result(ApplyResult(aid, "reverted", ""))
    assert [c[0] for c in rig.notifier.calls] == ["Glance undid a change"]


def test_revert_last_not_sent_when_disconnected(rig):
    rig.capture.connected = False
    rig.c.revert_last()
    assert rig.capture.reverts == 0


# ---------------------------------------------------------------- pause


def test_pause_and_timed_resume(rig):
    rig.settle()
    rig.c.open_panel()
    rig.c.toggle_pause(1800)
    assert rig.c.status()["paused"] and rig.tray.state == "paused"
    assert rig.overlay.chip.state is ChipState.PAUSED
    assert rig.overlay.panel is None
    assert rig.c.status()["pause_remaining_s"] == 1800
    rig.clock.advance(600_000)
    assert rig.c.status()["pause_remaining_s"] == 1200
    rig.clock.advance(1_200_000)
    assert not rig.c.status()["paused"]
    assert rig.c.status()["pause_remaining_s"] is None
    assert rig.tray.state == "active"
    rig.clock.advance(400)  # the field is checked again after the resume
    assert rig.overlay.chip.state is ChipState.FIXES


def test_toggle_pause_twice_resumes_and_cancels_the_timer(rig):
    rig.settle()
    rig.c.toggle_pause(60)
    rig.c.toggle_pause()
    assert not rig.c.status()["paused"]
    rig.c.toggle_pause()  # indefinite
    assert rig.c.status()["pause_remaining_s"] is None
    rig.clock.advance(60_000)  # the first pause's timer must not end this one
    assert rig.c.status()["paused"]
    rig.c.resume()
    assert not rig.c.status()["paused"]


def test_paused_does_not_call_harper(rig):
    rig.c.toggle_pause()
    rig.settle()
    rig.settle(text=TEH + " more", seq=2)
    assert rig.harper.calls == []
    assert rig.c.issues == ()
    assert rig.overlay.chip.state is ChipState.PAUSED
    rig.c.open_panel()
    assert rig.overlay.panel is None


def test_pause_drops_a_check_in_flight(tmp_path):
    rig = Rig(tmp_path, deferred=True)
    rig.c.handle_snapshot(snap())
    rig.clock.advance(80)
    rig.c.toggle_pause()
    rig.deliver()
    assert rig.c.issues == ()


# ---------------------------------------------------------------- dictionary / rules


def test_add_to_dictionary_removes_issue_and_persists(rig, tmp_path):
    rig.settle(text=MIXED)
    rig.c.add_to_dictionary(rig.issue("brwon").id)
    assert [r for r in (i.rule for i in rig.c.issues)] == ["Typo", "Typo"]
    assert rig.overlay.chip.label == "2"
    assert PersonalDictionary(tmp_path / "dictionary.txt").contains("brwon")
    rig.settle(text=MIXED + " brwon", seq=2)
    assert all(i.rule == "Typo" for i in rig.c.issues)


def test_ignore_rule_for_app_only(rig, tmp_path):
    rig.settle(text=MIXED)
    rig.c.ignore_rule(rig.issue("Teh").id, "app")
    assert [i.rule for i in rig.c.issues] == ["SpellCheck"]
    saved = IgnoredRules(tmp_path / "ignored.json")
    assert saved.is_ignored("Typo", "gedit") and not saved.is_ignored("Typo", "slack")
    rig.settle(text=MIXED, field_id="f2", wm_class="slack", seq=2)
    assert [i.rule for i in rig.c.issues] == ["Typo", "SpellCheck", "Typo"]


def test_ignore_rule_globally(rig, tmp_path):
    rig.settle(text=MIXED)
    rig.c.ignore_rule(rig.issue("brwon").id, "global")
    assert IgnoredRules(tmp_path / "ignored.json").is_ignored("SpellCheck", "slack")
    assert [i.rule for i in rig.c.issues] == ["Typo", "Typo"]
    with pytest.raises(ValueError):
        rig.c.ignore_rule(rig.issue("Teh").id, "everywhere")


# ---------------------------------------------------------------- focus / capture


def test_focus_lost_hides_everything(rig):
    rig.settle()
    rig.c.open_panel()
    rig.c.handle_focus_lost("blur")
    assert rig.overlay.chip is None and rig.overlay.panel is None
    assert rig.c.current is None and rig.c.status()["field"] is None
    rig.clock.advance(5000)
    assert rig.overlay.chip is None


def test_capture_disconnect_degrades(rig):
    rig.settle()
    rig.c.open_panel()
    rig.c.accept_issue(rig.issue("Teh").id)
    rig.c.handle_capture_disconnected()
    assert rig.overlay.chip is None and rig.overlay.panel is None
    assert rig.tray.state == "degraded"
    assert rig.c.status()["capture_connected"] is False
    rig.settle(seq=2)  # capture is back
    assert rig.tray.state == "active" and rig.overlay.chip.state is ChipState.FIXES
    rig.c.accept_issue(rig.issue("Teh").id)  # the lost apply no longer blocks
    assert len(rig.capture.applies) == 2


# ---------------------------------------------------------------- status


def test_status_contains_no_text(rig):
    rig.settle()
    st = rig.c.status()
    assert "quick" not in json.dumps(st)
    assert st == {
        "paused": False, "pause_remaining_s": None,
        "field": {"wm_class": "gedit", "gated": None, "len": len(TEH)},
        "local_issues": 1, "capture_connected": True, "harper_alive": True,
    }


def test_status_for_gated_field_and_extra_status(rig):
    rig.c.extra_status.append(lambda: {"ai": "ok"})
    rig.c.extra_status.append(lambda: {"hotkeys_grabbed": 4})
    rig.settle(gated="password", text="")
    st = rig.c.status()
    assert st["field"] == {"wm_class": "gedit", "gated": "password", "len": 0}
    assert (st["ai"], st["hotkeys_grabbed"]) == ("ok", 4)


# ---------------------------------------------------------------- extension points


def test_scheduler_generation_and_current_are_exposed(rig):
    assert rig.c.current is None
    g0 = rig.c.generation
    s = rig.settle()
    assert rig.c.current == s
    assert rig.c.generation == rig.c.scheduler.generation == g0 + 1


def test_on_analyze_due_gets_the_latest_snapshot(rig):
    due = []
    rig.c.on_analyze_due = lambda s, gen: due.append((s, gen))
    rig.c.handle_snapshot(snap(MIXED))
    rig.clock.advance(100)
    latest = snap(MIXED, seq=2, caret_rect=Rect(300, 400, 1, 18))
    rig.c.handle_snapshot(latest)
    rig.clock.advance(1200)
    assert due == [(latest, rig.c.generation)]


def test_chip_input_providers_are_merged_in_attach_order(rig):
    rig.c.extra_chip_inputs.append(lambda s: {"tone_label": "Blunt", "mismatch": True})
    rig.c.extra_chip_inputs.append(lambda s: {"tone_label": "Formal"})
    rig.settle()
    assert rig.overlay.chip.state is ChipState.TONE
    assert rig.overlay.chip.label == "Formal · 1"
    rig.c.extra_chip_inputs.append(lambda s: {"guard": "Check recipient"})
    rig.c.refresh()
    assert rig.overlay.chip.state is ChipState.GUARD


def test_mismatch_without_tone_label_falls_back_to_the_count(rig):
    rig.c.extra_chip_inputs.append(lambda s: {"mismatch": True})
    rig.settle()
    assert (rig.overlay.chip.state, rig.overlay.chip.label) == (ChipState.FIXES, "1")


def test_ai_none_shows_no_ai_in_tray(rig):
    rig.c.extra_chip_inputs.append(lambda s: {"ai": "none"})
    rig.settle()
    assert rig.tray.state == "no_ai"


def test_panel_providers_are_applied_in_attach_order(rig):
    rig.c.extra_panel.append(lambda s, v: replace(v, tone="blunt", banner="one"))
    rig.c.extra_panel.append(lambda s, v: replace(v, banner=f"{v.banner}+two"))
    rig.settle()
    rig.c.open_panel()
    assert (rig.overlay.panel.tone, rig.overlay.panel.banner) == ("blunt", "one+two")
    assert len(rig.overlay.panel.issues) == 1


def llm(start, end, rule="clarity"):
    return Issue(f"llm:{rule}:{start}:{end}", "llm", rule, "m", start, end, ("x",), False)


def test_extra_issues_join_the_list_and_lose_to_overlapping_harper(rig):
    rig.c.extra_issues.append(lambda s: [llm(0, 9), llm(10, 15)])  # "Teh quick", "brown"
    rig.c.extra_issues.append(lambda s: [llm(16, 19, "tone"), llm(90, 99)])  # "fox", outside
    rig.settle()
    assert [i.id for i in rig.c.issues] == ["harper:Typo:0:3", "llm:clarity:10:15",
                                            "llm:tone:16:19"]
    assert rig.c.status()["local_issues"] == 1
    assert rig.overlay.chip.label == "3"
    rig.c.accept_issue("llm:tone:16:19")
    assert rig.capture.applies[-1].edit == Edit(16, 19, "x")


def test_extra_ignored_rules_reach_the_filter(rig):
    rig.c.extra_ignored.append(lambda s: frozenset({"SpellCheck"}))
    rig.c.extra_ignored.append(lambda s: frozenset({"Typo"}) if s.wm_class == "slack"
                               else frozenset())
    rig.settle(text=MIXED)
    assert [i.rule for i in rig.c.issues] == ["Typo", "Typo"]
    rig.settle(text=MIXED, field_id="f2", wm_class="slack", seq=2)
    assert rig.c.issues == ()


def test_refresh_recomputes_issues_and_redraws(rig):
    found: list[Issue] = []
    rig.c.extra_issues.append(lambda s: list(found))
    rig.settle()
    rig.c.open_panel()
    found.append(llm(10, 15))
    assert rig.overlay.chip.label == "1"
    rig.c.refresh()
    assert rig.overlay.chip.label == "2" and len(rig.overlay.panel.issues) == 2


def test_providers_are_not_asked_about_gated_fields(rig):
    seen = []
    rig.c.extra_chip_inputs.append(lambda s: seen.append(s) or {})
    rig.c.extra_issues.append(lambda s: seen.append(s) or [])
    rig.c.extra_ignored.append(lambda s: seen.append(s) or frozenset())
    rig.settle(gated="password", text="")
    assert seen == []


def test_a_failing_provider_does_not_break_local_grammar(rig, capsys):
    def boom(s):
        raise RuntimeError("secret text")

    rig.c.extra_chip_inputs.append(boom)
    rig.c.extra_chip_inputs.append(lambda s: {"no_such_field": 1})
    rig.c.extra_issues.append(boom)
    rig.c.extra_ignored.append(boom)
    rig.c.extra_panel.append(boom)
    rig.c.extra_status.append(lambda: boom(None))
    rig.settle()
    rig.c.open_panel()
    assert rig.overlay.chip.label == "1" and len(rig.overlay.panel.issues) == 1
    assert rig.c.status()["local_issues"] == 1
    err = capsys.readouterr().err
    assert "hook_error" in err and "secret" not in err


class Listener:
    def __init__(self):
        self.events = []

    def on_snapshot(self, s):
        self.events.append(("snapshot", s.seq))

    def on_typing(self):
        self.events.append(("typing",))

    def on_focus_lost(self, reason):
        self.events.append(("focus_lost", reason))

    def on_apply_result(self, r):
        self.events.append(("apply_result", r.apply_id, r.status))

    def on_pause_changed(self, paused):
        self.events.append(("pause", paused))

    def on_capture_lost(self):
        self.events.append(("capture_lost",))


def test_listeners_hear_every_event(rig):
    full = Listener()

    class Partial:  # defines one hook only
        typed = 0

        def on_typing(self):
            self.typed += 1

    partial = Partial()
    rig.c.add_listener(full)
    rig.c.add_listener(partial)
    rig.c.handle_snapshot(snap())
    rig.c.handle_snapshot(snap(seq=2))  # same text: no typing
    rig.c.handle_apply_result(ApplyResult("other", "reverted", ""))
    rig.c.toggle_pause()
    rig.c.toggle_pause()
    rig.c.handle_focus_lost("blur")
    rig.c.handle_capture_disconnected()
    assert full.events == [
        ("snapshot", 1), ("typing",), ("snapshot", 2), ("apply_result", "other", "reverted"),
        ("pause", True), ("pause", False), ("snapshot", 2), ("typing",),
        ("focus_lost", "blur"), ("capture_lost",)]
    assert partial.typed == 2
    assert rig.notifier.calls == []  # somebody else's apply: the controller stays quiet


def test_listeners_get_no_snapshot_while_paused_or_gated(rig):
    seen = Listener()
    rig.c.add_listener(seen)
    rig.c.handle_snapshot(snap())
    rig.c.handle_snapshot(snap(gated="password", text="", seq=2))
    rig.c.handle_snapshot(snap(gated="password", text="", seq=3))
    # A field turning private looks like losing it, once; gated snapshots are not passed on.
    assert seen.events == [("snapshot", 1), ("typing",), ("focus_lost", "gated")]
    rig.c.handle_snapshot(snap(seq=4))
    assert seen.events[3:] == [("snapshot", 4), ("typing",)]
    rig.c.toggle_pause()
    rig.c.handle_snapshot(snap(TEH + " more", seq=5))
    assert seen.events[5:] == [("pause", True)]
    rig.c.resume()  # the listener catches up with the snapshot it missed
    assert seen.events[6:] == [("pause", False), ("snapshot", 5), ("typing",)]


def test_resume_on_a_gated_field_passes_no_snapshot(rig):
    seen = Listener()
    rig.c.add_listener(seen)
    rig.c.toggle_pause()
    rig.c.handle_snapshot(snap(gated="password", text=""))
    rig.c.resume()
    assert seen.events == [("pause", True), ("pause", False)]


def test_a_failing_listener_does_not_stop_the_others(rig, capsys):
    class Bad:
        def on_typing(self):
            raise RuntimeError("secret text")

    good = Listener()
    rig.c.add_listener(Bad())
    rig.c.add_listener(good)
    rig.settle()
    assert ("typing",) in good.events and rig.overlay.chip.label == "1"
    err = capsys.readouterr().err
    assert "hook_error" in err and "secret" not in err


def test_text_focused_before_harper_is_up_is_checked_once_it_is(rig):
    """glanced starts harper and capture together: the first snapshot can come first."""
    rig.harper.alive = False
    rig.settle()
    assert rig.c.issues == () and rig.harper.calls == []
    rig.harper.alive = True
    rig.c.recheck()
    assert [i.rule for i in rig.c.issues] == ["Typo"]
    assert rig.overlay.chip.state is ChipState.FIXES


def test_recheck_leaves_a_private_field_and_a_pause_alone(rig):
    rig.settle(gated="password")
    rig.c.recheck()
    assert rig.harper.calls == []
    rig.settle(field_id="f2")
    calls = len(rig.harper.calls)
    rig.c.toggle_pause()
    rig.c.recheck()
    assert len(rig.harper.calls) == calls


# ---------------------------------------------------------------- the suggestion strip


def test_strip_shows_only_the_corrected_sentence(rig):
    rig.cfg = AUTO  # the strip shows by itself
    rig.c.handle_snapshot(snap())
    assert rig.overlay.strip is None  # not settled yet
    rig.clock.advance(400)
    strip = rig.overlay.strip
    assert strip is not None and strip.text == "The quick brown fox jumps"
    assert [s.text for s in strip.segments if s.changed] == ["The"]
    assert strip.can_use and not strip.expanded and strip.note is None
    a = rig.overlay.strip_anchor
    assert (a.caret, a.field, a.window, a.pid) == (CARET, Rect(90, 190, 400, 300),
                                                   Rect(50, 50, 800, 600), 100)
    assert rig.overlay.panel is None


def test_nothing_to_fix_shows_no_strip(rig):
    rig.cfg = AUTO  # the strip shows by itself
    rig.settle(snap(text="The quick brown fox jumps"))
    assert rig.overlay.strip is None
    rig.settle(snap(text="Teh quick brwon fox"[:3] + "x" * 20))  # Teh: a safe fix
    assert rig.overlay.strip is not None
    rig.settle(snap(text="quick brwon fox jumps over"))  # a confident spelling fix
    assert rig.overlay.strip.text == "quick brown fox jumps over"
    rig.settle(snap(text="ask Smiht now"))  # only a spelling choice (maybe a name): not sure
    assert rig.overlay.strip is None


def test_use_applies_the_sentence_in_one_verified_edit(rig):
    rig.cfg = AUTO  # the strip shows by itself
    s = rig.settle(snap(text=MIXED))
    assert rig.overlay.strip.text == "The quick brown fox the end of it"
    rig.c.use_strip()
    (req,) = rig.capture.applies
    assert req.edit == merge_edits(MIXED, [Edit(0, 3, "The"), Edit(10, 15, "brown"),
                                           Edit(20, 23, "the")])
    assert req.expected_sha256 == text_sha256(s.text)
    assert rig.overlay.strip is None  # while it is applied: nothing more to use
    rig.c.use_strip()
    assert len(rig.capture.applies) == 1


def test_strip_hidden_when_paused_gated_or_snoozed(rig):
    rig.cfg = AUTO  # the strip shows by itself
    rig.settle()
    assert rig.overlay.strip is not None
    rig.c.toggle_pause()
    assert rig.overlay.strip is None
    rig.c.resume()
    rig.clock.advance(400)
    assert rig.overlay.strip is not None
    rig.c.open_panel()
    rig.c.close_panel(snooze=True)
    assert rig.overlay.strip is None
    rig.settle(snap(field_id="f2"))
    assert rig.overlay.strip is not None
    rig.c.handle_snapshot(snap(gated="password", field_id="f3"))
    assert rig.overlay.strip is None


def test_expanded_drawer_is_placed_by_the_same_anchor(rig):
    rig.settle()
    rig.c.open_panel()
    assert rig.overlay.strip.expanded is True
    assert rig.overlay.panel_anchor == rig.overlay.strip_anchor
    rig.c.use_strip()
    assert rig.overlay.panel is None  # Use closes the drawer


def test_extra_strip_adds_a_note_when_there_is_nothing_to_fix(rig):
    rig.cfg = AUTO  # the strip shows by itself
    rig.c.extra_strip.append(lambda s, v: replace(v, note="Reads blunt"))
    rig.settle(snap(text="The quick brown fox jumps"))
    assert rig.overlay.strip.note == "Reads blunt" and not rig.overlay.strip.can_use
    rig.c.use_strip()
    assert rig.capture.applies == []


def test_a_broken_strip_hook_is_skipped(rig):
    rig.cfg = AUTO  # the strip shows by itself
    def boom(_s, _v):
        raise RuntimeError

    rig.c.extra_strip.append(boom)
    rig.settle()
    assert rig.overlay.strip.text == "The quick brown fox jumps"


def test_enter_s_own_blur_does_not_lose_the_use(rig):
    rig.cfg = AUTO  # the strip shows by itself
    """Enter is a key grab: the app can report the grab's blur before the press arrives."""
    s = rig.settle()
    rig.c.handle_focus_lost("blur")
    rig.clock.advance(100)
    rig.c.use_strip()
    assert rig.capture.applies == []  # nothing typed while the field is away
    rig.c.handle_snapshot(snap(seq=2))  # the release: the same field, the same text
    (req,) = rig.capture.applies
    assert req.edit == Edit(0, 3, "The") and req.expected_sha256 == text_sha256(s.text)


def test_a_use_long_after_a_blur_or_for_other_text_does_nothing(rig):
    rig.settle()
    rig.c.handle_focus_lost("blur")
    rig.clock.advance(600)
    rig.c.use_strip()
    rig.c.handle_snapshot(snap(seq=2))
    assert rig.capture.applies == []
    rig.settle(snap(seq=3))
    rig.c.handle_focus_lost("blur")
    rig.c.use_strip()
    rig.c.handle_snapshot(snap(text=TEH + " more", seq=4))  # other words came back
    rig.c.handle_snapshot(snap(seq=5))
    assert rig.capture.applies == []
    rig.settle(snap(seq=6))
    rig.c.handle_focus_lost("blur")
    rig.c.use_strip()
    rig.clock.advance(2100)  # back too late
    rig.c.handle_snapshot(snap(seq=7))
    assert rig.capture.applies == []


def test_enter_after_the_blur_and_the_return_still_applies_what_was_shown(rig):
    rig.cfg = AUTO  # the strip shows by itself
    s = rig.settle(snap(wm_class="xed"))  # an app that leaves the method to Glance
    rig.c.handle_focus_lost("blur")
    rig.c.handle_snapshot(snap(seq=2, wm_class="xed"))  # back before the key reached glanced
    assert rig.overlay.strip is None  # not settled again yet
    rig.c.use_strip()
    (req,) = rig.capture.applies
    assert req.edit == Edit(0, 3, "The") and req.expected_sha256 == text_sha256(s.text)
    assert req.method == "paste"  # one undo step in every editor


def test_use_keeps_a_per_app_writeback_choice(rig):
    rig.cfg = AUTO  # the strip shows by itself
    from glance.config import AppCfg

    rig.cfg = replace(rig.cfg, apps={"gedit": AppCfg(writeback="type")})
    rig.settle()
    rig.c.use_strip()
    assert rig.capture.applies[-1].method == "auto"
