import json
import re
import time
from pathlib import Path

import pytest

from glance.backends.base import Analysis, BackendError, Ctx
from glance.backends.opencode import OpencodeBackend
from glance.backends.proc import ProcRunner
from glance.paths import runtime_dir

HERE = Path(__file__).parent
FAKE = str(HERE / "fake_cli" / "opencode")
CTX = Ctx("dm", "casual", "relaxed", "Slack")


@pytest.fixture(autouse=True)
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))


@pytest.fixture
def log(tmp_path):
    return tmp_path / "fake.log"


@pytest.fixture
def made():
    backends = []

    def make(log, mode="analyze", creds=1, **kw):
        env = {"FAKE_LOG": str(log), "FAKE_MODE": mode, "FAKE_CREDS": str(creds)}
        b = OpencodeBackend(ProcRunner(env=env), binary=FAKE, **kw)
        backends.append(b)
        return b

    yield make
    for b in backends:
        b.stop()


def records(log):
    return [json.loads(x) for x in log.read_text().splitlines()]


def kinds(log, first):
    return [r for r in records(log) if r["argv"][:len(first)] == first]


def test_server_started_once_and_reused(log, made):
    b = made(log)
    b.analyze("one", CTX)
    b.analyze("two", CTX)
    assert len(kinds(log, ["serve"])) == 1
    assert len(kinds(log, ["run"])) == 2
    run = kinds(log, ["run"])[0]["argv"]
    assert run[run.index("--attach") + 1] == "http://127.0.0.1:4096"
    assert run[run.index("--format") + 1] == "json" and "--pure" in run and "-m" not in run


def test_server_binds_loopback_with_password(log, made):
    b = made(log)
    b.analyze("hi", CTX)
    serve = kinds(log, ["serve"])[0]
    a = serve["argv"]
    assert a[a.index("--hostname") + 1] == "127.0.0.1" and a[a.index("--port") + 1] == "0"
    assert "--pure" in a
    cfg = json.loads(serve["config"])
    assert cfg["permission"]["*"] == "deny"
    assert all(cfg["permission"][k] == "deny" for k in ("bash", "read", "edit", "webfetch"))
    assert kinds(log, ["run"])[0]["config"] == serve["config"]
    assert re.fullmatch(r"[0-9a-f]{32}", serve["password"])
    assert kinds(log, ["run"])[0]["password"] == serve["password"]
    assert serve["password"] not in " ".join(a)
    assert Path(serve["cwd"]) == runtime_dir() / "empty"


def test_model_flag(log, made):
    made(log, model="anthropic/claude-x").analyze("hi", CTX)
    run = kinds(log, ["run"])[0]["argv"]
    assert run[run.index("-m") + 1] == "anthropic/claude-x"


def test_server_restarted_if_dead(log, made):
    b = made(log)
    b.analyze("one", CTX)
    b._server.terminate()
    b._server.wait()
    b.analyze("two", CTX)
    assert len(kinds(log, ["serve"])) == 2


def test_stop_terminates_server(log, made):
    b = made(log)
    b.analyze("one", CTX)
    proc = b._server
    b.stop()
    assert proc.poll() is not None


def test_session_deleted_after_success(log, made):
    made(log).analyze("hi", CTX)
    assert [r["argv"] for r in kinds(log, ["session"])] == [["session", "delete", "ses_fake1"]]
    list(made(log, "rewrite").rewrite("hi", "friendlier", CTX))
    assert len(kinds(log, ["session"])) == 2


def test_session_deleted_after_failure_and_timeout(log, made):
    with pytest.raises(BackendError) as ei:
        made(log, "error").analyze("hi", CTX)
    assert ei.value.kind == "unavailable"
    assert len(kinds(log, ["session"])) == 1
    with pytest.raises(BackendError) as ei:
        made(log, "hang")._run_json("S", "u", {"type": "object"}, 0.6)
    assert ei.value.kind == "timeout"
    deadline = time.monotonic() + 3
    while len(kinds(log, ["session"])) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert [r["argv"] for r in kinds(log, ["session"])][-1] == ["session", "delete", "ses_fake1"]


def test_invalid_json_repaired_once(log, made):
    a = made(log, "prose").analyze("hi", CTX)
    assert isinstance(a, Analysis)
    assert len(kinds(log, ["run"])) == 2 and len(kinds(log, ["session"])) == 2


def test_rewrite_text(log, made):
    assert list(made(log, "rewrite").rewrite("x", "friendlier", CTX)) == ["The quick brown fox."]


def test_text_never_in_argv(log, made):
    made(log).analyze("SECRET-MARKER text", CTX)
    for r in records(log):
        assert "SECRET-MARKER" not in " ".join(r["argv"])
    run = kinds(log, ["run"])[0]
    assert "SECRET-MARKER" in run["stdin"] and run["stdin"].startswith("<system>\n")


def test_health_no_credentials(log, made):
    h = made(log, creds=0).health()
    assert h.installed and h.signed_in is False and "opencode auth login" in h.detail
    h = made(log, creds=1).health()
    assert h.installed and h.signed_in is True and "experimental" in h.detail


def test_health_after_recorded_server_error_says_update(log, made):
    b = made(log, "error")
    with pytest.raises(BackendError):
        b.analyze("hi", CTX)
    h = b.health()
    assert h.signed_in is False and "reinstall" in h.detail


def test_health_not_installed():
    b = OpencodeBackend(ProcRunner(), binary="/nonexistent/opencode-bin")
    assert b.health().installed is False
