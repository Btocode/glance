import json
import threading

import pytest

from glance.cli import main, parse_duration
from glance.ipc import JsonLineServer


def test_parse_duration():
    assert [parse_duration(x) for x in ("30m", "2h", "45s", "10")] == [1800, 7200, 45, 600]


@pytest.mark.parametrize("bad", ["", "m", "-5m", "0", "0s", "1.5h", "10x", "ten", "5 m"])
def test_parse_duration_rejects(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)


@pytest.fixture
def ctl(tmp_path, monkeypatch):
    """A stand-in glanced: records control messages and answers them from `replies`."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    seen: list[tuple[str, dict]] = []
    replies: dict[str, dict] = {}
    got = threading.Event()

    def on_message(conn, msg_type, payload):
        seen.append((msg_type, {k: v for k, v in payload.items() if k != "id"}))
        conn.send(msg_type, {"id": payload["id"], "ok": True, **replies.get(msg_type, {})})
        got.set()

    server = JsonLineServer(tmp_path / "glance" / "ctl.sock", on_message)
    server.start()
    yield seen, replies
    server.stop()


def test_cli_status_when_daemon_down(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert main(["status"]) == 3
    captured = capsys.readouterr()
    assert captured.err.strip() == "glanced is not running" and captured.out == ""


@pytest.mark.parametrize("argv", [["pause"], ["pause", "5m"], ["resume"]])
def test_cli_pause_and_resume_when_daemon_down(argv, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert main(argv) == 3
    assert "glanced is not running" in capsys.readouterr().err


def test_cli_pause_sends_seconds(ctl):
    seen, _ = ctl
    assert main(["pause", "30m"]) == 0
    assert seen == [("pause", {"seconds": 1800})]


def test_cli_pause_without_duration_is_indefinite(ctl):
    seen, _ = ctl
    assert main(["pause"]) == 0
    assert seen == [("pause", {"seconds": None})]


def test_cli_pause_bad_duration_sends_nothing(ctl, capsys):
    seen, _ = ctl
    assert main(["pause", "soon"]) == 2
    assert seen == [] and "soon" in capsys.readouterr().err


def test_cli_resume(ctl):
    seen, _ = ctl
    assert main(["resume"]) == 0
    assert seen == [("resume", {})]


def test_cli_status_prints_one_line_per_value(ctl, capsys):
    seen, replies = ctl
    replies["status"] = {"paused": True, "pause_remaining_s": 60, "local_issues": 2,
                         "field": {"wm_class": "gedit", "gated": None, "len": 44},
                         "capture_connected": False}
    assert main(["status"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["paused: true", "pause_remaining_s: 60", "local_issues: 2",
                   "field.wm_class: gedit", "field.gated: none", "field.len: 44",
                   "capture_connected: false"]
    assert seen == [("status", {})]


def test_cli_status_json(ctl, capsys):
    _, replies = ctl
    replies["status"] = {"paused": False, "field": None}
    assert main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"paused": False, "field": None}


def test_cli_reports_a_refused_request(ctl, capsys):
    _, replies = ctl
    replies["pause"] = {"ok": False, "error": "bad seconds"}
    assert main(["pause", "1m"]) == 1
    assert "bad seconds" in capsys.readouterr().err


def test_cli_wrapper_subcommand_still_works(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ZDOTDIR", str(tmp_path))
    (tmp_path / ".zshrc").write_text("")
    assert main(["wrapper", "status"]) == 0
    assert capsys.readouterr().out.strip() == "absent"
