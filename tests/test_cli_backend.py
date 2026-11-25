import io
from types import SimpleNamespace

import pytest

from glance import cli

NONE_READY = {"choice": None, "backends": {
    "claude": {"installed": False, "signed_in": None, "detail": ""},
    "codex": {"installed": True, "signed_in": False, "detail": ""},
    "gemini": {"installed": False, "signed_in": None, "detail": ""},
    "opencode": {"installed": False, "signed_in": None, "detail": ""},
    "api": {"installed": True, "signed_in": False, "detail": ""}}}


@pytest.fixture
def fake_ctl(monkeypatch):
    """Stands in for glanced: records (type, payload) and answers from `.replies[type]`."""
    ctl = SimpleNamespace(seen=[], replies={}, timeouts=[])

    def request(msg_type, payload, timeout_s=3.0):
        ctl.seen.append((msg_type, payload))
        ctl.timeouts.append(timeout_s)
        reply = ctl.replies.get(msg_type)
        return None if reply is None else {"ok": True, **reply}

    monkeypatch.setattr(cli, "_request", request)
    return ctl


def test_backend_list_when_none_connected(fake_ctl, capsys):
    fake_ctl.replies["backend_list"] = NONE_READY
    assert cli.main(["backend", "list"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == ("No AI connected — run one of the commands below, "
                                   "then: glance backend use <name>")
    for cmd in ("claude", "codex login", "gemini", "opencode auth login",
                "glance backend set-key"):
        assert cmd in out
    assert "not installed" in out and "signed out" in out


def test_backend_list_when_one_ready(fake_ctl, capsys):
    reply = {**NONE_READY, "choice": "claude"}
    reply["backends"] = {**NONE_READY["backends"],
                         "claude": {"installed": True, "signed_in": True, "detail": ""}}
    fake_ctl.replies["backend_list"] = reply
    assert cli.main(["backend", "list"]) == 0
    out = capsys.readouterr().out
    assert not out.startswith("No AI connected")
    assert "claude" in out and "ready" in out and "(in use)" in out


def test_backend_commands_when_daemon_down(fake_ctl, capsys):
    assert cli.main(["backend", "list"]) == 3
    assert "glanced is not running" in capsys.readouterr().err


def test_backend_use_ok(fake_ctl, capsys):
    fake_ctl.replies["backend_use"] = {"name": "claude"}
    assert cli.main(["backend", "use", "claude"]) == 0
    assert fake_ctl.seen == [("backend_use", {"name": "claude"})]


def test_backend_use_not_ready_exits_2_with_command(fake_ctl, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_request", lambda *a, **k: {"ok": False, "error": "not_ready"})
    assert cli.main(["backend", "use", "codex"]) == 2
    err = capsys.readouterr().err
    assert "codex" in err and "codex login" in err


def test_backend_use_rejects_unknown_name(fake_ctl, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["backend", "use", "bard"])
    assert e.value.code == 2


def test_backend_test_pass_and_fail(fake_ctl, capsys):
    fake_ctl.replies["backend_test"] = {"name": "claude", "passed": True, "detail": "ok"}
    assert cli.main(["backend", "test"]) == 0
    assert fake_ctl.seen == [("backend_test", {"name": None})]
    assert fake_ctl.timeouts[-1] >= 30
    assert "claude" in capsys.readouterr().out
    fake_ctl.replies["backend_test"] = {"name": "gemini", "passed": False, "detail": "auth"}
    assert cli.main(["backend", "test", "gemini"]) == 1
    out = capsys.readouterr()
    assert "gemini" in out.err and "run `gemini` to authenticate" in out.err


def test_backend_set_key_reads_getpass_and_stores(fake_ctl, monkeypatch, capsys):
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "sk-secret-123")
    fake_ctl.replies["backend_key"] = {}
    fake_ctl.replies["ping"] = {}
    assert cli.main(["backend", "set-key", "--provider", "openai"]) == 0
    assert fake_ctl.seen[-1] == ("backend_key", {"provider": "openai", "key": "sk-secret-123"})
    captured = capsys.readouterr()
    assert "sk-secret-123" not in captured.out + captured.err


def test_backend_key_alias_and_empty_key(fake_ctl, monkeypatch, capsys):
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "  ")
    fake_ctl.replies["ping"] = {}
    assert cli.main(["backend", "key"]) == 2
    assert [t for t, _ in fake_ctl.seen] == ["ping"]


def test_set_key_checks_the_daemon_before_asking_for_the_key(fake_ctl, monkeypatch, capsys):
    def boom(prompt=""):
        raise AssertionError("asked for a key with no daemon")

    monkeypatch.setattr("getpass.getpass", boom)
    assert cli.main(["backend", "set-key"]) == 3
    assert "glanced is not running" in capsys.readouterr().err


def test_rewrite_pipes_stdin_to_stdout(fake_ctl, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("ok do it"))
    fake_ctl.replies["rewrite"] = {"text": "Sure, I'll do it."}
    assert cli.main(["rewrite", "--tone", "friendlier"]) == 0
    assert fake_ctl.seen == [("rewrite", {"tone": "friendlier", "text": "ok do it"})]
    assert fake_ctl.timeouts[-1] >= 30
    assert capsys.readouterr().out == "Sure, I'll do it.\n"


def test_rewrite_no_ai_exits_4(fake_ctl, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("ok do it"))
    monkeypatch.setattr(cli, "_request", lambda *a, **k: {"ok": False, "error": "none"})
    assert cli.main(["rewrite", "--tone", "friendlier"]) == 4
    captured = capsys.readouterr()
    assert captured.out == "" and "Connect an AI" in captured.err
    assert "glance backend list" in captured.err


def test_rewrite_other_failure_exits_1(fake_ctl, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("hi"))
    monkeypatch.setattr(cli, "_request", lambda *a, **k: {"ok": False, "error": "limit"})
    assert cli.main(["rewrite", "--tone", "shorter"]) == 1
    assert capsys.readouterr().out == ""


def test_rewrite_empty_stdin_exits_2(fake_ctl, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("  \n"))
    assert cli.main(["rewrite", "--tone", "shorter"]) == 2
    assert fake_ctl.seen == []


def test_profile_show_set_clear(fake_ctl, capsys):
    fake_ctl.replies["profile_show"] = {"profile": "team_channel", "key": "slack:eng"}
    assert cli.main(["profile"]) == 0
    assert "team_channel" in capsys.readouterr().out
    fake_ctl.replies["profile_set"] = {"profile": "dm"}
    assert cli.main(["profile", "set", "dm"]) == 0
    fake_ctl.replies["profile_clear"] = {}
    assert cli.main(["profile", "clear"]) == 0
    assert [t for t, _ in fake_ctl.seen] == ["profile_show", "profile_set", "profile_clear"]
    assert fake_ctl.seen[1][1] == {"name": "dm"}


def test_profile_without_focused_field(fake_ctl, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_request",
                        lambda *a, **k: {"ok": False, "error": "no focused field"})
    assert cli.main(["profile", "show"]) == 1
    assert "no focused field" in capsys.readouterr().err
