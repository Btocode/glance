from pathlib import Path

from glance import service
from glance.cli import main


def _calls(results):
    seen = []

    def run(argv):
        seen.append(list(argv))
        return results.pop(0)
    return seen, run


def test_already_running_runs_nothing(capsys):
    seen, run = _calls([])
    assert service.main(running=lambda: True, run=run, platform="linux") == 0
    assert seen == [] and "already running" in capsys.readouterr().out


def test_linux_starts_the_user_unit_and_waits(capsys):
    answers = iter([False, False, True])
    seen, run = _calls([0])
    code = service.main(running=lambda: next(answers), run=run, platform="linux",
                        sleep=lambda _s: None)
    assert code == 0 and seen == [["systemctl", "--user", "start", "glance.service"]]
    assert "Glance started" in capsys.readouterr().out


def test_failed_start_says_where_to_look(capsys):
    _, run = _calls([5])
    assert service.main(running=lambda: False, run=run, platform="linux") == 1
    assert "journalctl --user -u glance" in capsys.readouterr().err


def test_not_answering_in_time(capsys):
    _, run = _calls([0])
    assert service.main(running=lambda: False, run=run, platform="linux", wait_s=0) == 1
    assert "not answering" in capsys.readouterr().err


def test_macos_kickstarts_then_loads_the_agent():
    argvs = service.start_argvs("darwin", 501, Path("/Users/u"))
    assert argvs[0] == ["launchctl", "kickstart", "gui/501/io.github.btocode.glance"]
    assert argvs[1][:3] == ["launchctl", "bootstrap", "gui/501"]
    assert argvs[1][3].endswith("Library/LaunchAgents/io.github.btocode.glance.plist")
    answers = iter([False, True])
    seen, run = _calls([3, 0])
    assert service.main(running=lambda: next(answers), run=run, platform="darwin",
                        sleep=lambda _s: None) == 0
    assert [a[1] for a in seen] == ["kickstart", "bootstrap"]


def test_cli_start_when_running_does_not_touch_systemd(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(service, "_run", lambda argv: (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr("glance.cli._request", lambda *_a, **_k: {"ok": True})
    assert main(["start"]) == 0
    assert "already running" in capsys.readouterr().out
