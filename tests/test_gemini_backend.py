import json
from pathlib import Path

import pytest

from glance.backends.base import Analysis, BackendError, Ctx, extract_json_object
from glance.backends.gemini import GeminiBackend
from glance.backends.proc import ProcRunner
from glance.paths import runtime_dir

HERE = Path(__file__).parent
FAKE = str(HERE / "fake_cli" / "gemini")
CTX = Ctx("dm", "casual", "relaxed", "Slack")


@pytest.fixture(autouse=True)
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)


@pytest.fixture
def log(tmp_path):
    return tmp_path / "fake.log"


def make(log, mode="analyze", **kw):
    env = {"FAKE_LOG": str(log), "FAKE_MODE": mode}
    return GeminiBackend(ProcRunner(env=env), binary=FAKE, **kw)


def records(log):
    return [json.loads(x) for x in log.read_text().splitlines()]


def test_gemini_argv(log):
    make(log).analyze("hi", CTX)
    a = records(log)[0]["argv"]
    assert a[a.index("-p") + 1] == ""
    assert a[a.index("-o") + 1] == "json"
    assert a[a.index("--approval-mode") + 1] == "plan"
    assert "--skip-trust" in a and "-m" not in a
    assert a[a.index("-e") + 1] == "none"  # extensions off (unverified flag form)
    list(make(log, "rewrite", model="gemini-x").rewrite("hi", "friendlier", CTX))
    s = records(log)[1]["argv"]
    assert s[s.index("-o") + 1] == "stream-json" and s[s.index("-m") + 1] == "gemini-x"


def test_gemini_text_on_stdin_only(log):
    make(log).analyze("SECRET-MARKER text", CTX)
    rec = records(log)[0]
    assert "SECRET-MARKER" in rec["stdin"] and "SECRET-MARKER" not in " ".join(rec["argv"])
    assert rec["stdin"].startswith("<system>\n") and Path(rec["cwd"]) == runtime_dir() / "empty"


def test_gemini_fenced_json_is_extracted(log):
    a = make(log).analyze("Teh", CTX)
    assert isinstance(a, Analysis) and a.issues[0].quote == "Teh"
    assert len(records(log)) == 1


def test_gemini_prose_then_repair(log):
    a = make(log, "prose").analyze("Teh", CTX)
    assert isinstance(a, Analysis) and len(records(log)) == 2


def test_gemini_rewrite_stream(log):
    assert "".join(make(log, "rewrite").rewrite("x", "friendlier", CTX)) == "The quick brown fox."


def test_gemini_recorded_failure_is_auth(log):
    # recorded shape: IneligibleTierError UNSUPPORTED_CLIENT (also mentions "Resource has been exhausted")
    with pytest.raises(BackendError) as ei:
        make(log, "error").analyze("hi", CTX)
    assert ei.value.kind == "auth"
    with pytest.raises(BackendError) as ei:
        list(make(log, "error").rewrite("hi", "friendlier", CTX))
    assert ei.value.kind == "auth"


def test_gemini_classify():
    from glance.backends.gemini import classify_error

    assert classify_error("Error: Resource has been exhausted (e.g. check quota).") == "rate_limit"
    assert classify_error("IneligibleTierError: UNSUPPORTED_CLIENT") == "auth"
    assert classify_error("boom") == "unavailable"


def test_gemini_timeout(log):
    with pytest.raises(BackendError) as ei:
        make(log, "hang")._run_json("S", "u", {"type": "object"}, 0.4)
    assert ei.value.kind == "timeout"


def test_gemini_health_unknown_when_no_creds(log):
    h = make(log).health()
    assert h.installed and h.signed_in is None


def test_gemini_health_creds_never_mean_ready(log, tmp_path):
    (tmp_path / "home" / ".gemini").mkdir(parents=True)
    (tmp_path / "home" / ".gemini" / "oauth_creds.json").write_text("{}")
    h = make(log).health()
    assert h.signed_in is None and "Test" in h.detail


def test_gemini_health_after_recorded_failure_is_not_connected(log, tmp_path):
    (tmp_path / "home" / ".gemini").mkdir(parents=True)
    (tmp_path / "home" / ".gemini" / "oauth_creds.json").write_text("{}")
    b = make(log, "error")
    with pytest.raises(BackendError):
        b.analyze("hi", CTX)
    h = b.health()
    assert h.installed and h.signed_in is False
    assert "gemini" in h.detail and "API key" in h.detail


def test_gemini_health_not_installed():
    b = GeminiBackend(ProcRunner(), binary="/nonexistent/gemini-bin")
    assert b.health().installed is False


def test_extract_json_object():
    assert extract_json_object('x {"a": {"b": 1}} y') == {"a": {"b": 1}}
    assert extract_json_object('```json\n{"a": "}"}\n```') == {"a": "}"}
    assert extract_json_object('{bad} {"ok": 1}') == {"ok": 1}
    for bad in ("no json", "[1, 2]", ""):
        with pytest.raises(BackendError) as ei:
            extract_json_object(bad)
        assert ei.value.kind == "bad_output"
