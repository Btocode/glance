"""The `test_state` / `test_apply` ctl messages: only with GLANCE_TEST=1, never any other way."""
from __future__ import annotations

from dataclasses import dataclass, field

from glance.daemon.test_hooks import handle_test_message
from glance.protocol import ApplyResult, Edit, FieldSnapshot
from glance.ui.chip_model import ChipInputs


def snap(text: str = "Teh cat") -> FieldSnapshot:
    return FieldSnapshot(
        app="app", wm_class="app", title="t", url=None, role="entry", text=text, caret=3,
        caret_rect=None, prev_char_rect=None, field_rect=None, window_rect=None,
        field_id="f1", capture_confidence=1.0, seq=4, pid=1, gated=None)


@dataclass
class StubController:
    current: FieldSnapshot | None = None
    applied: list = field(default_factory=list)
    ready: bool = True

    issues: tuple = ()

    def handle_apply_result(self, r):
        self.seen = getattr(self, "seen", []) + [r]

    def _chip_inputs(self, s):
        return ChipInputs(has_field=s is not None, text_len=len(s.text) if s else 0,
                          settled=True)

    def request_apply(self, edit: Edit, method: str = "auto", expected_sha256=None) -> str:
        self.applied.append((edit, method))
        return "a1" if self.ready else ""


def test_everything_is_refused_without_glance_test(monkeypatch):
    monkeypatch.delenv("GLANCE_TEST", raising=False)
    c = StubController(snap())
    assert handle_test_message(c, "test_state", {}) is None
    assert handle_test_message(c, "test_apply", {"start": 0, "end": 3, "replacement": "The",
                                                 "method": "auto"}) is None
    assert c.applied == []


def test_other_messages_are_not_ours(monkeypatch):
    monkeypatch.setenv("GLANCE_TEST", "1")
    assert handle_test_message(StubController(), "status", {}) is None


def test_test_state_returns_the_snapshot_with_text_and_the_chip(monkeypatch):
    monkeypatch.setenv("GLANCE_TEST", "1")
    reply = handle_test_message(StubController(snap()), "test_state", {})
    assert reply["ok"] is True
    assert FieldSnapshot.from_dict(reply["snapshot"]) == snap()
    assert reply["snapshot"]["text"] == "Teh cat"
    assert reply["chip"] == {"state": "hidden", "label": "", "badge": None}  # 7 < 15 chars
    assert reply["issues"] == []


def test_test_state_without_a_field(monkeypatch):
    monkeypatch.setenv("GLANCE_TEST", "1")
    reply = handle_test_message(StubController(None), "test_state", {})
    assert reply["ok"] and reply["snapshot"] is None


def test_test_apply_sends_the_edit(monkeypatch):
    monkeypatch.setenv("GLANCE_TEST", "1")
    c = StubController(snap())
    reply = handle_test_message(
        c, "test_apply", {"start": 0, "end": 3, "replacement": "The", "method": "paste"})
    assert reply == {"ok": True, "apply_id": "a1"}
    assert c.applied == [(Edit(0, 3, "The"), "paste")]


def test_test_apply_reports_nothing_sent_and_bad_input(monkeypatch):
    monkeypatch.setenv("GLANCE_TEST", "1")
    c = StubController(snap(), ready=False)
    good = {"start": 0, "end": 3, "replacement": "The", "method": "auto"}
    assert handle_test_message(c, "test_apply", good) == {"ok": False, "error": "not sent"}
    for bad in ({}, {**good, "start": "x"}, {**good, "method": "nope"},
                {**good, "replacement": 5}):
        assert handle_test_message(c, "test_apply", bad) == {"ok": False, "error": "bad request"}


def test_apply_result_is_recorded_and_still_reaches_the_controller(monkeypatch):
    monkeypatch.setenv("GLANCE_TEST", "1")
    c = StubController(snap())
    assert handle_test_message(c, "test_apply_result", {"apply_id": "a1"}) == {
        "ok": True, "result": None}
    c.handle_apply_result(ApplyResult("a1", "verified", ""))  # the daemon's call
    assert handle_test_message(c, "test_apply_result", {"apply_id": "a1"}) == {
        "ok": True, "result": {"apply_id": "a1", "status": "verified", "detail": ""}}
    assert c.seen == [ApplyResult("a1", "verified", "")]
