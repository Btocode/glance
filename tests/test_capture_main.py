import json

from glance.capture.main import Serialized, handle_command, printable
from glance.protocol import (
    APPLY,
    APPLY_RESULT,
    FOCUS_LOST,
    PING,
    REVERT,
    SNAPSHOT,
    ApplyRequest,
    ApplyResult,
    Edit,
)


def test_printable_never_contains_the_text():
    line = printable(SNAPSHOT, {"text": "my secret draft", "caret": 3, "gated": None})
    obj = json.loads(line)
    assert "secret" not in line
    assert obj["t"] == SNAPSHOT and obj["p"]["text"].startswith("len=15,sha=")
    assert obj["p"]["caret"] == 3


def test_printable_with_text_for_tests():
    obj = json.loads(printable(SNAPSHOT, {"text": "Teh quick"}, with_text=True))
    assert obj["p"]["text"] == "Teh quick"


def test_printable_focus_lost():
    assert json.loads(printable(FOCUS_LOST, {"reason": "blur"})) == {
        "t": FOCUS_LOST, "p": {"reason": "blur"}}


def test_apply_fails_until_writeback_exists():
    req = ApplyRequest("a1", "1:2:3", "00", Edit(0, 3, "The"), "auto")
    t, p = handle_command(APPLY, {**req.to_dict(), "id": 7})
    assert t == APPLY_RESULT and p["id"] == 7
    assert ApplyResult.from_dict(p) == ApplyResult("a1", "failed", "writeback unavailable")


def test_revert_fails_until_writeback_exists():
    t, p = handle_command(REVERT, {"apply_id": "a1"})
    assert t == APPLY_RESULT and "id" not in p
    assert ApplyResult.from_dict(p).status == "failed"


def test_malformed_apply_still_gets_a_failed_result():
    t, p = handle_command(APPLY, {"apply_id": 5})
    assert t == APPLY_RESULT
    assert ApplyResult.from_dict(p) == ApplyResult("", "failed", "writeback unavailable")


class FakeEngine:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error
        self.applied, self.reverts = [], 0

    def apply(self, req):
        self.applied.append(req)
        if self.error is not None:
            raise self.error
        return self.result

    def revert_last(self):
        self.reverts += 1
        if self.error is not None:
            raise self.error
        return self.result


def test_apply_goes_to_the_engine_and_its_result_comes_back():
    req = ApplyRequest("a1", "1:2:3", "00", Edit(0, 3, "The"), "auto")
    engine = FakeEngine(ApplyResult("a1", "verified", ""))
    t, p = handle_command(APPLY, {**req.to_dict(), "id": 7}, engine)
    assert engine.applied == [req] and engine.reverts == 0
    assert t == APPLY_RESULT and p["id"] == 7
    assert ApplyResult.from_dict(p) == ApplyResult("a1", "verified", "")


def test_revert_goes_to_the_engine():
    engine = FakeEngine(ApplyResult("a1", "reverted", ""))
    t, p = handle_command(REVERT, {"id": 8}, engine)
    assert engine.reverts == 1 and engine.applied == []
    assert t == APPLY_RESULT and p["id"] == 8 and p["status"] == "reverted"


def test_malformed_apply_never_reaches_the_engine():
    engine = FakeEngine(ApplyResult("a1", "verified", ""))
    bad = ApplyRequest("a1", "1:2:3", "00", Edit(0, 3, "The"), "auto").to_dict()
    bad["edit"]["replacement"] = 5
    for payload in ({"apply_id": "a1"}, bad, {"apply_id": "a1", "edit": "x"}):
        _, p = handle_command(APPLY, payload, engine)
        assert ApplyResult.from_dict(p) == ApplyResult("a1", "failed", "bad request")
    assert engine.applied == []


def test_engine_error_still_answers():
    engine = FakeEngine(error=RuntimeError("x connection lost"))
    req = ApplyRequest("a1", "1:2:3", "00", Edit(0, 3, "The"), "auto")
    for msg_type in (APPLY, REVERT):
        t, p = handle_command(msg_type, {**req.to_dict(), "id": 9}, engine)
        result = ApplyResult.from_dict(p)
        assert (t, p["id"], result.apply_id, result.status) == (APPLY_RESULT, 9, "a1", "failed")
        assert "x connection" not in result.detail


def test_other_messages_need_no_reply():
    assert handle_command(PING, {}) is None


def test_serialized_runs_reentrant_calls_afterwards_in_order():
    log: list[str] = []

    def handle(name: str) -> None:
        log.append(f"{name} start")
        if name == "a":
            run("b")  # arrives while "a" is being handled
            run("c")
        log.append(f"{name} end")

    run = Serialized(handle)
    run("a")
    assert log == ["a start", "a end", "b start", "b end", "c start", "c end"]


def test_serialized_recovers_after_an_error():
    calls: list[str] = []

    def handle(name: str) -> None:
        calls.append(name)
        if name == "bad":
            raise RuntimeError

    run = Serialized(handle)
    try:
        run("bad")
    except RuntimeError:
        pass
    run("ok")
    assert calls == ["bad", "ok"]


def test_snapshots_feed_the_typing_watch_but_gated_ones_do_not():
    from glance.capture.main import watch_snapshot
    from glance.protocol import FOCUS_LOST, SNAPSHOT
    from glance.writeback.activity import TypingWatch

    t = [10.0]
    w = TypingWatch(now=lambda: t[0])
    watch_snapshot(w, SNAPSHOT, {"field_id": "f", "text": "ab"})
    watch_snapshot(w, SNAPSHOT, {"field_id": "f", "text": "", "gated": "password"})
    watch_snapshot(w, FOCUS_LOST, {"reason": "blur"})
    assert w.quiet_s() == float("inf")
    watch_snapshot(w, SNAPSHOT, {"field_id": "f", "text": "abc"})
    assert w.quiet_s() == 0.0
