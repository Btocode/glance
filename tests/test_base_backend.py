import pytest

from glance.backends.base import Analysis, BackendError, BaseBackend, Ctx, Health
from glance.backends.prompts import ANALYZE_SCHEMA, REPAIR_SUFFIX, analyze_system, user_block

CTX = Ctx("dm", "casual", "relaxed", "Slack")
VALID = {"tone": "casual", "mismatch": False, "reason": None, "issues": [], "flags": []}


class Stub(BaseBackend):
    name = "stub"

    def __init__(self, results, deltas=()):
        self.results, self.calls, self.deltas, self.stream_calls = list(results), [], deltas, []

    def _run_json(self, system, user, schema, timeout_s):
        self.calls.append((system, user, schema, timeout_s))
        r = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        return r

    def _run_stream(self, system, user, timeout_s):
        self.stream_calls.append((system, user, timeout_s))
        yield from self.deltas

    def cancel(self):
        pass

    def health(self):
        return Health(True, True, "ok")


def test_analyze_happy_path():
    b = Stub([VALID])
    a = b.analyze("hi there", CTX)
    assert isinstance(a, Analysis) and a.tone == "casual"
    system, user, schema, t = b.calls[0]
    assert system == analyze_system(CTX) and user == user_block("hi there")
    assert schema is ANALYZE_SCHEMA and t == 8.0


def test_base_backend_repairs_once_then_raises():
    b = Stub([{"tone": 1}, VALID])
    assert b.analyze("x", CTX).tone == "casual"
    assert len(b.calls) == 2
    assert not b.calls[0][0].endswith(REPAIR_SUFFIX) and b.calls[1][0].endswith(REPAIR_SUFFIX)

    bad = Stub([{"tone": 1}])
    with pytest.raises(BackendError) as e:
        bad.analyze("x", CTX)
    assert e.value.kind == "bad_output" and len(bad.calls) == 2


def test_other_errors_are_not_retried():
    class Boom(Stub):
        def _run_json(self, *a):
            self.calls.append(a)
            raise BackendError("timeout")

    b = Boom([])
    with pytest.raises(BackendError) as e:
        b.analyze("x", CTX)
    assert e.value.kind == "timeout" and len(b.calls) == 1


def test_rewrite_streams_deltas():
    b = Stub([], deltas=["Thanks, ", "will do."])
    assert "".join(b.rewrite("ok", "friendlier", CTX)) == "Thanks, will do."
    _system, user, t = b.stream_calls[0]
    assert user == user_block("ok") and t == 20.0 and "friendlier" not in user


def test_backend_error_kinds():
    with pytest.raises(ValueError):
        BackendError("nonsense")
    assert BackendError("auth", "signed out").kind == "auth"
