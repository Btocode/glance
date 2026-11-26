import json
from pathlib import Path

import pytest

from glance.backends.base import CONNECT, Analysis, BackendError, Ctx, Health
from glance.backends.claude import ClaudeBackend
from glance.backends.proc import ProcRunner

HERE = Path(__file__).parent
FAKE = str(HERE / "fake_cli" / "claude")
CTX = Ctx("dm", "casual", "relaxed", "Slack")


@pytest.fixture(autouse=True)
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))


@pytest.fixture
def log(tmp_path):
    return tmp_path / "fake.log"


def make(log, mode="analyze", auth="in", **kw):
    env = {"FAKE_LOG": str(log), "FAKE_MODE": mode, "FAKE_AUTH": auth}
    return ClaudeBackend(ProcRunner(env=env), binary=FAKE, **kw)


def records(log):
    return [json.loads(x) for x in log.read_text().splitlines()]


def test_claude_argv_is_exact():
    a = ClaudeBackend(ProcRunner()).argv_json("SYS", {"type": "object"})
    assert a[:2] == ["claude", "-p"]
    for flag, val in (
        ("--model", "haiku"), ("--tools", ""), ("--setting-sources", ""),
        ("--output-format", "json"), ("--system-prompt", "SYS"),
    ):
        assert a[a.index(flag) + 1] == val
    assert "--strict-mcp-config" in a and "--no-session-persistence" in a
    # The schema is spelled out in the prompt: --json-schema costs a second model turn.
    assert "--json-schema" not in a
    s = ClaudeBackend(ProcRunner()).argv_stream("SYS")
    assert s[:2] == ["claude", "-p"]
    assert s[s.index("--output-format") + 1] == "stream-json"
    assert "--include-partial-messages" in s and "--verbose" in s
    assert s[s.index("--tools") + 1] == "" and s[s.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in s and "--no-session-persistence" in s
    assert "--json-schema" not in s


def test_connect_table():
    assert CONNECT == {
        "claude": "claude",
        "codex": "codex login",
        "gemini": "gemini",
        "opencode": "opencode auth login",
        "api": "glance backend set-key",
    }


def test_text_goes_on_stdin_never_argv(log):
    make(log).analyze("SECRET-MARKER text here", CTX)
    rec = records(log)[0]
    assert "SECRET-MARKER" in rec["stdin"] and "SECRET-MARKER" not in " ".join(rec["argv"])
    list(make(log, "rewrite").rewrite("SECRET-MARKER-2 text", "friendlier", CTX))
    rec = records(log)[1]
    assert "SECRET-MARKER-2" in rec["stdin"] and "SECRET-MARKER-2" not in " ".join(rec["argv"])


def test_analyze_parses_recorded_shape(log):
    a = make(log).analyze("Teh quick brown fox", CTX)
    assert isinstance(a, Analysis)
    assert isinstance(a.tone, str) and isinstance(a.issues, tuple) and len(a.issues) == 4
    assert a.issues[0].quote == "Teh"


def test_rewrite_streams_deltas_from_recorded_shape(log):
    deltas = list(make(log, "rewrite").rewrite("Teh quick", "friendlier", CTX))
    assert len(deltas) == 4
    assert "".join(deltas) == "The quick brown fox jumps over the lazy dog. I think it's fine!"
    assert "--include-partial-messages" in records(log)[0]["argv"]


def test_error_shape_maps_to_kind(log):
    with pytest.raises(BackendError) as ei:
        make(log, "error").analyze("hi", CTX)
    assert ei.value.kind == "unavailable"  # api_error_status 404, model not found
    with pytest.raises(BackendError) as ei:
        list(make(log, "error").rewrite("hi", "friendlier", CTX))
    assert ei.value.kind == "unavailable"


@pytest.mark.parametrize("status,text,kind", [
    (401, "Invalid API key. Please run /login", "auth"),
    (None, "Not logged in - Please run /login", "auth"),
    (429, "Claude usage limit reached", "rate_limit"),
    (None, "You've hit your rate limit", "rate_limit"),
    (500, "overloaded", "unavailable"),
])
def test_classify_error_text(status, text, kind):
    from glance.backends.claude import classify_error

    assert classify_error(status, text) == kind


def test_timeout_maps(log):
    b = make(log, "hang")
    with pytest.raises(BackendError) as ei:
        b._run_json("S", "u", {"type": "object"}, 0.4)
    assert ei.value.kind == "timeout"


def test_health_signed_in_and_out(log):
    h = make(log, auth="in").health()
    assert h.installed and h.signed_in is True
    h = make(log, auth="out").health()
    assert h.installed and h.signed_in is False
    h = make(log, auth="junk").health()
    assert h.installed and h.signed_in is None


def test_health_reads_the_real_cli_indented_json(log):
    """`claude auth status --json` prints indented JSON over many lines."""
    h = make(log, auth="pretty").health()
    assert h.installed and h.signed_in is True


def test_health_not_installed():
    b = ClaudeBackend(ProcRunner(), binary="/nonexistent/claude-bin")
    assert b.health() == Health(False, None, b.health().detail)
    assert b.health().installed is False and b.health().signed_in is None


def test_prewarm_is_noop_until_warm_is_turned_on(log):
    b = make(log)  # a backend nobody turned warm on for never holds a process
    b.prewarm("analyze")
    b.wait_warm()
    assert b._idle == {}


def test_prewarm_used_once_then_replaced(log):
    b = make(log, "rewrite", warm=True)
    b.prewarm("rewrite")
    b.wait_warm()
    first = b._idle["rewrite"]
    assert first.alive
    b.prewarm("rewrite")  # at most one per kind: same process kept
    b.wait_warm()
    assert b._idle["rewrite"] is first
    assert "".join(b.rewrite("hello", "friendlier", CTX))
    recs = records(log)
    assert "--input-format" in recs[0]["argv"]
    content = json.loads(recs[0]["stdin"])["message"]["content"]
    assert content.count("hello") == 1 and content.startswith("<system>")  # the call's prompt
    b.wait_warm()
    assert b._idle["rewrite"] is not first  # replaced after use
    b.cancel()
    b.close()
    assert b._idle == {}


def test_analyze_never_uses_the_warm_session(log):
    """Analyze never goes to the stream-json input session (its system prompt rides in the
    message); its own spare is a `-p` call with the real system prompt (test_speed_path)."""
    b = make(log, warm=True)
    b.prewarm("analyze")
    b.wait_warm()
    assert "analyze" not in b._idle
    assert isinstance(b.analyze("hello", Ctx("email", "formal", "strict", "Gmail")), Analysis)
    argv = records(log)[0]["argv"]
    assert "--input-format" not in argv and "--json-schema" not in argv
    b.close()


def test_analyze_reads_json_from_the_result_text(log):
    from glance.backends.claude import ClaudeBackend as CB

    obj = {"type": "result", "result": '```json\n{"corrected": "Hi.", "tone": "calm", '
           '"mismatch": false, "reason": null, "suggestions": [], "flags": []}\n```'}
    assert CB._structured(obj)["corrected"] == "Hi."


def test_warm_flag_off_disables_prewarm(log):
    b = make(log, warm=False)
    b.prewarm("analyze", "S")
    b.wait_warm()
    assert b._idle == {}


def test_empty_model_leaves_out_dash_dash_model(log):
    b = ClaudeBackend(ProcRunner(), model="", warm=True)
    for argv in (b.argv_json("S", {}), b.argv_stream("S"), b._argv_warm("analyze"),
                 b._argv_warm("rewrite")):
        assert "--model" not in argv and "" in argv  # "" is --tools' value, never a model
        assert argv[argv.index("--tools") + 1] == ""
