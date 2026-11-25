"""EditSession: the logic behind the Ctrl+G window, without GTK."""
from __future__ import annotations

import pytest

from glance.audience.profiles import BUILTIN
from glance.backends.budget import Budget
from glance.backends.supervisor import BackendSupervisor
from glance.config import BackendCfg, Config
from glance.daemon.rewrite import RewriteOutcome
from glance.features.edit_session import EditSession, EditSessionHost, prompt_ctx
from glance.grammar.filters import IssueFilter
from glance.grammar.store import IgnoredRules, PersonalDictionary
from tests.fakes import FakeBackend, FakeClock, FakeHarper

SECRET = "sk-ant-api03-AbCdEf0123456789xyzQRSTUV"


class Deferred:
    """`run_bg` that runs nothing until `run()`: work then done, in order."""

    def __init__(self) -> None:
        self.jobs: list = []

    def __call__(self, work, done) -> None:
        self.jobs.append((work, done))

    def run(self) -> None:
        while self.jobs:
            work, done = self.jobs.pop(0)
            done(work())


class Rig:
    def __init__(self, tmp_path, deltas=("Fix the bug in [which file?]",)) -> None:
        self.clock = FakeClock()
        self.cfg = Config(backend=BackendCfg(choice="claude"))
        self.backend = FakeBackend("claude", deltas=deltas)
        self.sup = BackendSupervisor(
            {"claude": self.backend}, lambda: self.cfg.backend,
            Budget(self.clock, lambda: self.cfg.backend, tmp_path / "usage.json"),
            self.clock, lambda: False, lambda st: None)
        self.harper = FakeHarper({"teh": ("SpellCheck", ("the",)),
                                  "an bug": ("AnA", ("a bug",)),
                                  "a apple": ("AnA", ("an apple",))})
        self.filter = IssueFilter(PersonalDictionary(tmp_path / "dictionary.txt"),
                                  IgnoredRules(tmp_path / "ignored.json"), [])
        self.bg = Deferred()

    def session(self, text: str) -> EditSession:
        return EditSession(text, self.harper, self.filter, self.sup, prompt_ctx(), self.bg)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_prompt_ctx_is_the_ai_prompt_profile():
    ctx = prompt_ctx()
    p = BUILTIN["ai_prompt"]
    assert (ctx.profile, ctx.target_tone, ctx.strictness, ctx.app_label) == (
        "ai_prompt", p.target_tone, p.strictness, "claude-code")


def test_chips_put_sharpen_first_then_custom(rig):
    s = rig.session("x")
    assert [k for k, _ in s.chips] == ["sharpen_prompt", "add_structure", "shorter", "custom"]
    assert s.chips[0][1] == "Sharpen prompt"


def test_session_accept_edits_buffer(rig):
    s = rig.session("fix teh bug")
    s.accept(s.issues()[0].id)
    assert s.text == "fix the bug"
    assert s.issues() == []


def test_issues_recomputed_on_change(rig):
    s = rig.session("fix teh bug")
    assert [i.rule for i in s.issues()] == ["SpellCheck"]
    s.set_text("eat a apple")
    assert [(i.start, i.end) for i in s.issues()] == [(4, 11)]


def test_issues_respect_profile_ignores_and_filter(rig, tmp_path):
    rig.harper.patterns["fix"] = ("SentenceCapitalization", ("Fix",))
    s = rig.session("fix teh bug")
    assert [i.rule for i in s.issues()] == ["SpellCheck"]  # ai_prompt ignores capitalisation


def test_grammar_failure_degrades_to_no_issues(rig):
    class Broken:
        def check(self, text):
            raise TimeoutError("harper is down")

    s = EditSession("fix teh bug", Broken(), rig.filter, rig.sup, prompt_ctx(), rig.bg)
    assert s.issues() == []


def test_accept_unknown_issue_is_ignored(rig):
    s = rig.session("fix teh bug")
    s.accept("harper:Nope:0:1")
    assert s.text == "fix teh bug"


def test_fix_all_safe_applies_only_safe_ones(rig):
    s = rig.session("fix teh bug, an bug and a apple")
    s.fix_all_safe()
    assert s.text == "fix teh bug, a bug and an apple"  # spelling is never "safe"
    s.undo()
    assert s.text == "fix teh bug, an bug and a apple"


def test_session_preview_and_apply_preview_and_undo(rig):
    s = rig.session("fix teh bug")
    seen = []
    s.preview("sharpen_prompt", on_update=seen.append)
    rig.bg.run()
    assert s.outcome == RewriteOutcome("Fix the bug in [which file?]", True, None)
    assert seen and seen[-1].outcome == s.outcome and seen[-1].tone == "sharpen_prompt"
    (call,) = [c for c in rig.backend.calls if c.kind == "rewrite"]
    assert call.tone == "sharpen_prompt" and call.ctx == prompt_ctx()
    assert s.apply_preview() is True
    assert s.text == "Fix the bug in [which file?]" and s.outcome is None
    s.undo()
    assert s.text == "fix teh bug"


def test_session_preview_dropped_if_text_changed(rig):
    s = rig.session("fix teh bug")
    seen = []
    s.preview("sharpen_prompt", on_update=seen.append)
    s.set_text("fix teh bug now")
    rig.bg.run()
    assert s.outcome is None and all(u.outcome is None for u in seen)
    assert s.apply_preview() is False and s.text == "fix teh bug now"


def test_a_newer_preview_wins(rig):
    s = rig.session("fix teh bug")
    s.preview("sharpen_prompt")
    s.preview("shorter")
    rig.bg.run()
    assert s.preview_tone == "shorter" and s.outcome is not None


def test_session_refuses_unapplicable_outcome(tmp_path):
    rig = Rig(tmp_path, deltas=("Fix it, ask [[GLANCE_E9]]",))  # an invented placeholder
    s = rig.session("fix teh bug for sam@example.com")
    s.preview("sharpen_prompt")
    rig.bg.run()
    assert s.outcome is not None and s.outcome.reason == "placeholders"
    assert s.outcome.text  # still offered as "Copy rewrite"
    assert s.apply_preview() is False and s.text == "fix teh bug for sam@example.com"


def test_session_text_is_shielded_for_backend(rig):
    s = rig.session(f"deploy with key {SECRET} to staging")
    s.preview("sharpen_prompt")
    rig.bg.run()
    (call,) = [c for c in rig.backend.calls if c.kind == "rewrite"]
    assert SECRET not in call.text and "[[GLANCE_S" in call.text


def test_custom_preview_passes_instruction(rig):
    s = rig.session("fix teh bug")
    s.preview("custom", custom="as a haiku")
    rig.bg.run()
    (call,) = [c for c in rig.backend.calls if c.kind == "rewrite"]
    assert call.tone == "custom" and call.custom == "as a haiku"


def test_undo_is_single_level_and_typing_clears_it(rig):
    s = rig.session("fix teh bug")
    s.undo()
    assert s.text == "fix teh bug"
    s.accept(s.issues()[0].id)
    s.set_text("fix the bug!")
    s.undo()
    assert s.text == "fix the bug!"


# ---------------------------------------------------------------- the daemon side


class FakeWindows:
    def __init__(self) -> None:
        self.opened: list = []
        self.presented = 0

    def open(self, session, over, on_done):
        self.opened.append((session, over, on_done))

    def present(self):
        self.presented += 1


def make_host(rig, windows, rect=None):
    return EditSessionHost(rig.harper, rig.filter, rig.sup, rig.bg,
                           open_window=windows.open, present_window=windows.present,
                           window_rect=lambda xid: rect)


def test_host_opens_a_window_and_replies_on_done(rig):
    from glance.protocol import Rect

    windows, replies = FakeWindows(), []
    host = make_host(rig, windows, Rect(10, 20, 800, 600))
    host.handle({"text": "fix teh bug", "window": 77}, replies.append)
    (session, over, on_done), = windows.opened
    assert session.text == "fix teh bug" and over == Rect(10, 20, 800, 600)
    assert replies == [] and host.busy
    on_done("apply", "Fix the bug.")
    assert replies == [{"action": "apply", "text": "Fix the bug."}] and not host.busy
    on_done("apply", "again")  # a second call is ignored
    assert len(replies) == 1


def test_host_cancel_and_editor_replies(rig):
    windows, replies = FakeWindows(), []
    host = make_host(rig, windows)
    host.handle({"text": "a", "window": 0}, replies.append)
    windows.opened[-1][2]("cancel", "")
    host.handle({"text": "b", "window": 0}, replies.append)
    windows.opened[-1][2]("editor", "b!")
    assert replies == [{"action": "cancel"}, {"action": "editor", "text": "b!"}]


def test_host_one_window_at_a_time(rig):
    windows, first, second = FakeWindows(), [], []
    host = make_host(rig, windows)
    host.handle({"text": "one", "window": 0}, first.append)
    host.handle({"text": "two", "window": 0}, second.append)
    assert len(windows.opened) == 1 and windows.presented == 1
    assert second == [{"action": "cancel"}] and first == []


def test_host_rejects_a_bad_request(rig):
    windows, replies = FakeWindows(), []
    host = make_host(rig, windows)
    host.handle({"text": 5}, replies.append)
    assert replies == [{"ok": False, "error": "bad request"}] and windows.opened == []


def test_host_survives_a_reply_to_a_gone_client(rig):
    windows = FakeWindows()
    host = make_host(rig, windows)

    def gone(reply):
        raise ConnectionError("peer gone")

    host.handle({"text": "x", "window": 0}, gone)
    windows.opened[-1][2]("apply", "y")
    assert not host.busy


def test_host_answers_when_the_window_cannot_open(rig):
    replies = []

    def broken(session, over, on_done):
        raise RuntimeError("no display")

    host = EditSessionHost(rig.harper, rig.filter, rig.sup, rig.bg, open_window=broken,
                           present_window=lambda: None, window_rect=lambda xid: None)
    host.handle({"text": "x", "window": 0}, replies.append)
    assert replies == [{"ok": False, "error": "window failed"}] and not host.busy
