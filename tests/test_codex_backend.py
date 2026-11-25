import json
from pathlib import Path

import pytest

from glance.backends.base import Analysis, BackendError, Ctx
from glance.backends.codex import CodexBackend
from glance.backends.proc import ProcRunner
from glance.backends.prompts import ANALYZE_SCHEMA
from glance.paths import runtime_dir

HERE = Path(__file__).parent
FAKE = str(HERE / "fake_cli" / "codex")
CTX = Ctx("dm", "casual", "relaxed", "Slack")


@pytest.fixture(autouse=True)
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))


@pytest.fixture
def log(tmp_path):
    return tmp_path / "fake.log"


def make(log, mode="analyze", auth="in", **kw):
    env = {"FAKE_LOG": str(log), "FAKE_MODE": mode, "FAKE_AUTH": auth}
    return CodexBackend(ProcRunner(env=env), binary=FAKE, **kw)


def records(log):
    return [json.loads(x) for x in log.read_text().splitlines()]


def test_codex_argv_is_exact(log):
    make(log).analyze("hi", CTX)
    a = records(log)[0]["argv"]
    i = a.index("exec")
    assert a[i:i + 6] == ["exec", "--json", "--ephemeral", "--skip-git-repo-check", "-s", "read-only"]
    assert a[-1] == "-" and "-m" not in a
    assert "--ignore-rules" in a and "--ignore-user-config" in a
    off = [a[j + 1] for j, x in enumerate(a) if x == "--disable"]
    assert {"shell_tool", "unified_exec", "view_image"} <= set(off)
    assert 'web_search="disabled"' in a
    schema = a[a.index("--output-schema") + 1]
    assert Path(schema) == runtime_dir() / "analyze.schema.json"
    assert json.loads(records(log)[0]["schema"]) == ANALYZE_SCHEMA


def test_codex_model_flag_only_when_set(log):
    make(log, model="gpt-x").analyze("hi", CTX)
    a = records(log)[0]["argv"]
    assert a[a.index("-m") + 1] == "gpt-x" and a[-1] == "-"


def test_codex_rewrite_has_no_schema(log):
    list(make(log, "rewrite").rewrite("hi", "friendlier", CTX))
    assert "--output-schema" not in records(log)[0]["argv"]


def test_codex_text_on_stdin_only(log):
    make(log).analyze("SECRET-MARKER text", CTX)
    list(make(log, "rewrite").rewrite("SECRET-MARKER-2 text", "friendlier", CTX))
    for rec, marker in zip(records(log), ("SECRET-MARKER", "SECRET-MARKER-2")):
        assert marker in rec["stdin"] and marker not in " ".join(rec["argv"])
        assert rec["stdin"].startswith("<system>\n") and "\n</system>\n\n<glance_text>" in rec["stdin"]
        assert Path(rec["cwd"]) == runtime_dir() / "empty"


def test_codex_analyze_parses_recorded_shape(log):
    a = make(log).analyze("Teh quick brown fox", CTX)
    assert isinstance(a, Analysis) and len(a.issues) == 4 and a.issues[0].quote == "Teh"


def test_codex_stream_or_single_chunk(log):
    deltas = list(make(log, "rewrite").rewrite("Teh quick", "friendlier", CTX))
    assert deltas == ["The quick brown fox jumps over the lazy dog. I think it looks fine!"]


def test_codex_error_shape(log):
    with pytest.raises(BackendError) as ei:
        make(log, "error").analyze("hi", CTX)
    assert ei.value.kind == "unavailable"  # model_not_found
    with pytest.raises(BackendError) as ei:
        list(make(log, "error").rewrite("hi", "friendlier", CTX))
    assert ei.value.kind == "unavailable"


def test_codex_timeout(log):
    with pytest.raises(BackendError) as ei:
        make(log, "hang")._run_json("S", "u", {"type": "object"}, 0.4)
    assert ei.value.kind == "timeout"


@pytest.mark.parametrize("text,kind", [
    ("Not logged in. Run codex login", "auth"),
    ('{"status":401}', "auth"),
    ("You've hit your usage limit", "rate_limit"),
    ("model_not_found", "unavailable"),
])
def test_codex_classify(text, kind):
    from glance.backends.codex import classify_error

    assert classify_error(text) == kind


def test_codex_is_rewrite_only_by_default():
    assert CodexBackend.analyze_default is False


def test_codex_health(log):
    h = make(log, auth="in").health()
    assert h.installed and h.signed_in is True and "experimental" in h.detail
    h = make(log, auth="out").health()
    assert h.installed and h.signed_in is False and "codex login" in h.detail
    b = CodexBackend(ProcRunner(), binary="/nonexistent/codex-bin")
    assert b.health().installed is False
