"""The fast check path: the claude CLI's spare analyze process (started on focus, the exact
argv of the next call, primed so the CLI keeps waiting, refilled, capped, torn down), the
streamed analyze (partial corrected text, no wait for the process to exit), `--effort low`
with its fallback, the API backend's prepared connection, and the strip filling in while
the AI's answer streams."""
from __future__ import annotations

import json
import time

import pytest

from glance.backends.base import Analysis, BackendError, Ctx
from glance.backends.claude import PRIMER, ClaudeBackend
from glance.backends.partial import CorrectedWatcher, analyze_progress, partial_string
from glance.backends.proc import ProcRunner
from glance.backends.prompts import analyze_system, user_block
from tests import test_ai_analyze as ai_t
from tests.test_check_shortcut import USER, USER_AI, USER_HARPER, AiRig, _shown
from tests.test_claude_backend import FAKE, records, runtime  # noqa: F401

CTX = Ctx("dm", "casual", "relaxed", "Slack")
ANSWER = {"corrected": "Hey how are you doing?", "tone": "casual", "mismatch": False,
          "reason": None, "suggestions": [], "flags": []}


@pytest.fixture
def log(tmp_path):
    return tmp_path / "fake.log"


def make(log, warm=True, **env):
    base = {"FAKE_LOG": str(log), "FAKE_MODE": "analyze", "FAKE_AUTH": "in",
            "FAKE_RESULT": json.dumps(ANSWER), "FAKE_FIRST_BYTE_S": "0.3"}
    return ClaudeBackend(ProcRunner(env={**base, **env}), binary=FAKE, warm=warm)


def wait_until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


# ---------------------------------------------------------------- the spare process


def test_prepare_starts_one_spare_with_the_exact_argv_and_sends_nothing(log):
    b = make(log)
    b.prepare(CTX)
    b.wait_warm()
    spare = b._idle["analyze"]
    assert spare.argv == b.argv_analyze(analyze_system(CTX)) and spare.primer == PRIMER
    time.sleep(0.6)  # past the CLI's wait for a first stdin byte: the primer keeps it waiting
    assert spare.alive
    assert not log.exists()  # no request was made: nothing of any text went anywhere
    b.close()
    assert not spare.alive and b._idle == {}


def test_check_uses_the_spare_then_refills(log):
    b = make(log)
    b.prepare(CTX)
    b.wait_warm()
    spare = b._idle["analyze"]
    a = b.analyze("hey how are yo doing", CTX)
    assert isinstance(a, Analysis) and a.corrected == ANSWER["corrected"]
    (rec,) = records(log)
    assert rec["stdin"] == user_block("hey how are yo doing")  # primer + rest: exact
    assert rec["argv"] == b.argv_analyze(analyze_system(CTX))[1:]
    b.wait_warm()
    refill = b._idle["analyze"]
    assert refill is not spare and refill.alive and refill.argv == spare.argv
    b.close()


def test_never_more_than_one_spare(log):
    b = make(log)
    b.prewarm("analyze", "SYSTEM A")
    b.wait_warm()
    first = b._idle["analyze"]
    b.prewarm("analyze", "SYSTEM A")  # same argv: kept
    b.wait_warm()
    assert b._idle["analyze"] is first
    b.prewarm("analyze", "SYSTEM B")  # another prompt: the old one goes
    b.wait_warm()
    second = b._idle["analyze"]
    assert second is not first and not first.alive and second.alive
    b.close()


def test_a_spare_for_another_prompt_is_not_used(log):
    b = make(log)
    b.prewarm("analyze", "SOME OTHER SYSTEM")
    b.wait_warm()
    other = b._idle["analyze"]
    b.analyze("hello there", CTX)
    assert not other.alive  # stopped, never fed
    (rec,) = records(log)
    assert rec["argv"][rec["argv"].index("--system-prompt") + 1] == analyze_system(CTX)
    b.wait_warm()
    assert b._idle["analyze"].argv == b.argv_analyze(analyze_system(CTX))  # refilled for it
    b.close()


def test_idle_spare_is_stopped(log):
    b = make(log)
    b.spare_idle_s = 0.2
    b.prepare(CTX)
    b.wait_warm()
    spare, warm = b._idle["analyze"], b._idle["rewrite"]
    assert wait_until(lambda: "analyze" not in b._idle and "rewrite" not in b._idle)
    assert wait_until(lambda: not spare.alive and not warm.alive)
    b.prepare(CTX)  # the next focus starts one again
    b.wait_warm()
    assert b._idle["analyze"].alive
    b.close()


def test_warm_off_means_no_spare_and_turning_it_off_kills(log):
    b = make(log, warm=False)
    b.prepare(CTX)
    b.wait_warm()
    assert b._idle == {}
    b.analyze("hi", CTX)
    b.wait_warm()
    assert b._idle == {}  # no refill either
    on = make(log)
    on.prepare(CTX)
    on.wait_warm()
    spare = on._idle["analyze"]
    on.set_warm(False)
    assert not spare.alive and on._idle == {}


def test_repair_prompt_does_not_take_the_refill(log):
    from glance.backends.prompts import REPAIR_SUFFIX

    b = make(log)
    b.prewarm("analyze", "S")
    b.wait_warm()
    b._run_json("S" + REPAIR_SUFFIX, user_block("x"), {}, 5.0)
    b.wait_warm()
    assert "analyze" not in b._idle  # the next check's prompt is not the repair's
    b.close()


# ---------------------------------------------------------------- streaming


def test_partial_string_reads_what_has_arrived():
    assert partial_string('{"corr', "corrected") is None
    assert partial_string('{"corrected": ', "corrected") is None
    assert partial_string('{"corrected": "Hey ho', "corrected") == ("Hey ho", False)
    assert partial_string('{"corrected":"a\\"b\\', "corrected") == ('a"b', False)
    assert partial_string('{"corrected": "x\\ny\\u00e9\\u00', "corrected") == ("x\nyé", False)
    assert partial_string('{"corrected": "\\ud83d\\ude00!"', "corrected") == ("\U0001F600!", True)
    assert partial_string('```json\n{"corrected": "Done.", "tone"', "corrected") == ("Done.", True)


def test_watcher_reports_longer_prefixes_only():
    seen = []
    with analyze_progress(seen.append):
        w = CorrectedWatcher()
        for t in ('{"corrected": "He', '{"corrected": "He', '{"corrected": "Hey h',
                  '{"corrected": "Hey h\\', '{"corrected": "Hey ho"', '{"corrected": "Hey ho", "x'):
            w.feed(t)
    assert seen == ["He", "Hey h", "Hey ho"]
    CorrectedWatcher().feed('{"corrected": "a')  # no listener: nothing happens


def test_analyze_streams_the_corrected_text_and_skips_the_exit_wait(log):
    b = make(log, warm=False, FAKE_CHUNK="5", FAKE_LINGER="3")
    seen = []
    start = time.monotonic()
    with analyze_progress(seen.append):
        a = b.analyze("hey how are yo doing", CTX)
    took = time.monotonic() - start
    assert a.corrected == ANSWER["corrected"]
    assert len(seen) > 2 and seen[-1] == ANSWER["corrected"]
    assert all(ANSWER["corrected"].startswith(p) for p in seen)
    assert took < 2.5  # the fake lingers 3 s after its `result` line, as the CLI does
    assert b.take_usage() is not None  # usage still comes from the result line


# ---------------------------------------------------------------- --effort


def test_every_call_runs_at_low_effort(log):
    b = make(log, warm=False)
    for argv in (b.argv_analyze("S"), b.argv_stream("S"), b._argv_warm("rewrite"),
                 b.argv_vision("S")):
        assert argv[argv.index("--effort") + 1] == "low"


def test_a_cli_without_effort_gets_the_call_again_without_it(log):
    b = make(log, warm=False, FAKE_NO_EFFORT="1")
    a = b.analyze("hey", CTX)
    assert a.corrected == ANSWER["corrected"] and b.effort == ""
    assert "--effort" not in records(log)[-1]["argv"]
    with pytest.raises(BackendError):  # any other failure is still one
        make(log, warm=False, FAKE_MODE="error").analyze("hey", CTX)


# ---------------------------------------------------------------- API connection


def test_api_prepare_opens_a_connection_without_key_or_text():
    import httpx

    from glance.backends.api import ApiBackend

    sent = []

    def handler(req):
        sent.append(req)
        return httpx.Response(404)

    class Keys:
        def __init__(self, key):
            self.key = key

        def get(self, _provider):
            return self.key

    b = ApiBackend(Keys(None), client=httpx.Client(transport=httpx.MockTransport(handler)))
    b.prepare(CTX)
    assert sent == []  # no key: nothing to get ready for
    b = ApiBackend(Keys("sk-secret"), client=httpx.Client(transport=httpx.MockTransport(handler)))
    b.prepare(CTX)
    b.prepare(CTX)  # once per PREPARE_EVERY_S
    (req,) = sent
    assert req.method == "HEAD" and req.content == b""
    assert "sk-secret" not in str(req.headers) and "x-api-key" not in req.headers


# ---------------------------------------------------------------- the strip


def test_strip_fills_in_while_the_answer_streams(tmp_path):
    r = AiRig(tmp_path, analysis=USER_AI, patterns=USER_HARPER, delay_ms=2000)
    orig = r.backend.analyze

    def streaming(text, ctx):
        w = CorrectedWatcher()
        w.feed('{"corrected": "Hey how are you doing, se')
        return orig(text, ctx)

    r.backend.analyze = streaming
    r.c.handle_snapshot(ai_t.snap(USER))
    r.c.check_now()
    r.clock.advance(50)
    strip = r.overlay.strip
    assert strip.loading is not None and not strip.can_use  # Use waits for the whole answer
    assert "Hey how are you doing," in _shown(r)  # the AI's fixes so far, in place
    assert "alright, how to get in place" in _shown(r)  # the rest: the quick local fixes
    r.clock.advance(2000)
    assert r.overlay.strip.loading is None and r.overlay.strip.can_use
    assert _shown(r) == USER_AI.corrected


def test_a_partial_answer_for_old_text_is_dropped(tmp_path):
    r = AiRig(tmp_path, analysis=USER_AI, patterns=USER_HARPER, delay_ms=2000)
    r.c.handle_snapshot(ai_t.snap(USER))
    r.c.check_now()
    r.clock.advance(50)
    job = r.ai._job
    r.c.handle_snapshot(ai_t.snap(USER + " more"))  # typed: another generation
    from glance.daemon.ai import _provisional
    from glance.privacy.redact import shield

    issues = _provisional(shield(USER), "Hey how are you", 0)
    assert issues
    r.ai._show_partial(job, issues)
    assert job.partial == ()


def test_identical_recheck_is_answered_from_the_cache(tmp_path):
    r = AiRig(tmp_path, analysis=USER_AI, patterns=USER_HARPER, delay_ms=0)
    r.c.handle_snapshot(ai_t.snap(USER))
    r.c.check_now()
    r.clock.advance(50)
    r.c.check_now()  # the same text again
    r.clock.advance(0)
    assert [c.kind for c in r.backend.calls] == ["analyze"]
    assert r.overlay.strip.loading is None and _shown(r) == USER_AI.corrected


def test_focus_prepares_the_backend_with_the_context_only(tmp_path):
    r = AiRig(tmp_path, analysis=USER_AI)
    got = []
    r.sup.prepare = got.append
    r.c.handle_snapshot(ai_t.snap(USER))
    r.c.handle_snapshot(ai_t.snap(USER + " x"))  # typing in the same field: once
    assert len(got) == 1 and isinstance(got[0], Ctx)
    assert USER not in repr(got[0])
    assert r.backend.calls == []  # shortcut mode: nothing was sent
    r.c.handle_snapshot(ai_t.snap(USER, field_id="f2"))
    assert len(got) == 2
