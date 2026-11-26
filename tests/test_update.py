"""glance update: version lookup, `--check`, the doctor line and the tray launcher (no network)."""
from pathlib import Path

from glance import cli, update
from glance.config import load_config
from glance.doctor.checks import CHECKS, Updates

A, B = "a" * 40, "b" * 40


def fake_run(installed=A, latest=B):
    def run(argv, env, timeout):
        if "rev-parse" in argv:
            return None if installed is None else installed + "\n"
        if "ls-remote" in argv:
            assert env["GIT_TERMINAL_PROMPT"] == "0"
            return None if latest is None else f"{latest}\trefs/heads/main\n"
        raise AssertionError(argv)
    return run


def git_home(tmp_path):
    (tmp_path / ".local/share/glance-src/.git").mkdir(parents=True)
    return {"HOME": str(tmp_path)}


def test_src_dir_default_and_override(tmp_path):
    assert update.default_src_dir({"HOME": str(tmp_path)}) == tmp_path / ".local/share/glance-src"
    assert update.default_src_dir({"GLANCE_SRC": "/x/y", "HOME": "/h"}) == Path("/x/y")


def test_check_states(tmp_path):
    env = git_home(tmp_path)
    assert update.check(fake_run(A, A), env) == ("current", A, A)
    assert update.check(fake_run(A, B), env) == ("available", A, B)
    assert update.check(fake_run(A, None), env)[0] == "unknown"
    assert update.check(fake_run(None, B), env)[0] == "unknown"


def test_no_git_dir_is_unknown_without_lookup(tmp_path):
    def boom(*a):
        raise AssertionError("no lookup expected")
    assert update.check(boom, {"HOME": str(tmp_path)})[0] == "unknown"


def test_check_never_raises(tmp_path):
    def boom(*a):
        raise RuntimeError
    assert update.check(boom, git_home(tmp_path))[0] == "unknown"


def test_report_lines_and_codes():
    assert update.report("available", A, B) == (
        "update available (installed aaaaaaa, latest bbbbbbb)", 10)
    assert update.report("current", A, A) == ("up to date", 0)
    assert update.report("unknown", None, None) == ("unknown (not a git install)", 0)


def test_cli_check_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(update, "check", lambda: ("available", A, B))
    assert cli.main(["update", "--check"]) == 10
    assert "update available" in capsys.readouterr().out


def test_cli_update_runs_installer_and_passes_exit_code(monkeypatch):
    seen = []

    class P:
        def __init__(self, argv):
            seen.append(argv)

        def wait(self):
            return 7

    monkeypatch.setattr(update, "installer_argv", lambda: ["fake-installer"])
    monkeypatch.setattr(update.subprocess, "Popen", P)
    assert cli.main(["update"]) == 7
    assert seen == [["fake-installer"]]


def test_installer_is_the_published_one():
    assert update.INSTALL_URL == "https://raw.githubusercontent.com/Btocode/glance/main/install.sh"


def test_config_default_and_override(tmp_path):
    assert load_config(tmp_path / "none.toml").general.check_updates is True
    p = tmp_path / "c.toml"
    p.write_text("[general]\ncheck_updates = false\n")
    assert load_config(p).general.check_updates is False


def test_doctor_updates_line():
    from tests.test_doctor import make_env
    chk = Updates()
    assert chk in CHECKS or any(c.id == "updates" for c in CHECKS)
    env = make_env(Path("/nonexistent-home"))
    for status, level in (("current", "ok"), ("available", "warn"), ("unknown", "ok")):
        env.update_status = lambda s=status: s
        assert chk.run(env).status == level
    env.update_status = lambda: 1 / 0
    assert chk.run(env).status == "ok"


def test_update_state_caches():
    st = update.UpdateState(lambda: "available")
    assert st.status == "unknown"
    st.refresh()
    assert st.status == "available"


def test_launch_update_notifies(tmp_path):
    notes, done = [], []

    class P:
        def wait(self):
            return 1

    import threading
    ev = threading.Event()
    ok = update.launch_update(lambda s, b: notes.append(s), tmp_path / "s/update.log",
                              popen=lambda *a, **k: P(),
                              on_done=lambda r: (done.append(r), ev.set()))
    assert ok and ev.wait(2)
    assert notes == ["Glance update failed"] and done == [False]
    assert (tmp_path / "s/update.log").exists()


def test_launch_update_start_failure(tmp_path):
    notes = []

    def bad(*a, **k):
        raise FileNotFoundError

    assert not update.launch_update(lambda s, b: notes.append(s), tmp_path / "u.log", popen=bad)
    assert notes == ["Glance update failed"]
