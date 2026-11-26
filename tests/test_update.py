"""glance update: version lookup, `--check`, the doctor line and the tray launcher (no network)."""
import json
from pathlib import Path

import pytest

from glance import cli, update
from glance.config import load_config
from glance.doctor.checks import CHECKS, Updates

A, B = "a" * 40, "b" * 40


def fake_run(installed=A, latest=B, branch="main"):
    def run(argv, env, timeout):
        if "rev-parse" in argv:
            return None if installed is None else installed + "\n"
        if "symbolic-ref" in argv:
            return None if branch is None else branch + "\n"
        if "ls-remote" in argv:
            assert env["GIT_TERMINAL_PROMPT"] == "0"
            return None if latest is None else f"{latest}\trefs/heads/main\n"
        raise AssertionError(argv)
    return run


def releases(*tags, prerelease=(), draft=()):
    body = [{"tag_name": t, "prerelease": t in prerelease, "draft": t in draft} for t in tags]

    def fetch(url, timeout):
        assert url == update.RELEASES_API
        return json.dumps(body)
    return fetch


def no_fetch(url, timeout):
    raise AssertionError("no release lookup expected")


def offline(url, timeout):
    return None


def no_run(*a):
    raise AssertionError("no git lookup expected")


def git_home(tmp_path):
    (tmp_path / ".local/share/glance-src/.git").mkdir(parents=True)
    return {"HOME": str(tmp_path)}


def test_src_dir_default_and_override(tmp_path):
    assert update.default_src_dir({"HOME": str(tmp_path)}) == tmp_path / ".local/share/glance-src"
    assert update.default_src_dir({"GLANCE_SRC": "/x/y", "HOME": "/h"}) == Path("/x/y")


def test_main_channel_compares_commits(tmp_path):
    env = git_home(tmp_path)
    c = update.check(fake_run(A, A), env, no_fetch, "0.4.0")
    assert (c.status, c.channel, c.commit, c.latest) == ("current", "main", A, A)
    c = update.check(fake_run(A, B), env, no_fetch, "0.4.0")
    assert c.status == "available" and c.line() == (
        "update available: Glance 0.4.0 (main aaaaaaa) → main bbbbbbb")
    assert c.label == "Update available (main) — Update now"
    assert update.check(fake_run(A, None), env, no_fetch).status == "unknown"


def test_release_channel_compares_versions(tmp_path):
    env = {"HOME": str(tmp_path)}  # a tarball or package install: no clone
    fetch = releases("core-v0.9.0", "v0.5.0", "v0.4.0", "v0.6.0rc1", "v0.7.0",
                     prerelease=("v0.6.0rc1",), draft=("v0.7.0",))
    c = update.check(no_run, env, fetch, "0.4.0")
    assert (c.status, c.latest, c.channel) == ("available", "0.5.0", "release")
    assert c.line() == "update available: Glance 0.4.0 → 0.5.0"
    assert c.label == "Update to Glance 0.5.0 — Update now"
    c = update.check(no_run, env, fetch, "0.5.0")
    assert c.status == "current" and c.line() == "up to date: Glance 0.5.0"
    assert update.check(no_run, env, fetch, "0.6.0").status == "current"  # ahead (a dev build)


def test_tag_checkout_follows_releases(tmp_path):
    env = git_home(tmp_path)
    c = update.check(fake_run(A, B, branch=None), env, releases("v0.5.0"), "0.4.0")
    assert (c.status, c.channel, c.latest) == ("available", "release", "0.5.0")


def test_no_release_yet_falls_back_to_main_commit(tmp_path):
    c = update.check(fake_run(A, B, branch=None), git_home(tmp_path), releases(), "0.4.0")
    assert (c.status, c.channel) == ("available", "main")
    assert update.check(no_run, {"HOME": str(tmp_path / "x")}, releases(), "0.4.0"
                        ).status == "unknown"


def test_offline_or_garbage_is_unknown(tmp_path):
    env = {"HOME": str(tmp_path)}
    for fetch in (offline, lambda u, t: "{not json", lambda u, t: '{"message": "rate"}'):
        c = update.check(no_run, env, fetch, "0.4.0")
        assert c.status == "unknown" and "could not check" in c.line()


def test_check_never_raises(tmp_path):
    def boom(*a):
        raise RuntimeError
    assert update.check(boom, git_home(tmp_path), boom).status == "unknown"


def test_opt_out_env_makes_no_lookup(tmp_path):
    env = {**git_home(tmp_path), "GLANCE_NO_UPDATE_CHECK": "1"}
    assert update.check(no_run, env, no_fetch).status == "unknown"


def test_report_lines_and_codes():
    up = update.UpdateCheck("available", "0.4.0", "0.5.0")
    assert update.report(up) == ("update available: Glance 0.4.0 → 0.5.0", 10)
    assert update.report(update.UpdateCheck("current", "0.4.0", "0.4.0"))[1] == 0
    assert update.report(update.UpdateCheck("unknown", "0.4.0"))[1] == 0


def test_cli_check_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(update, "check", lambda: update.UpdateCheck("available", "0.4.0",
                                                                     "0.5.0"))
    assert cli.main(["update", "--check"]) == 10
    assert "Glance 0.4.0 → 0.5.0" in capsys.readouterr().out


class _P:
    seen: list = []  # noqa: RUF012 - reset by each test

    def __init__(self, argv):
        _P.seen.append(argv)

    def wait(self):
        return 7


def test_cli_update_installs_the_latest_release_by_default(monkeypatch):
    monkeypatch.setattr(update, "installer_argv", lambda ref="latest": ["fake-installer", ref])
    monkeypatch.setattr(update.subprocess, "Popen", _P)
    monkeypatch.setattr(update, "packaged_by", lambda: None)
    _P.seen = []
    assert cli.main(["update"]) == 7
    assert cli.main(["update", "--main"]) == 7
    assert _P.seen == [["fake-installer", "latest"], ["fake-installer", "main"]]


def test_cli_update_of_a_package_install_points_at_the_package_manager(monkeypatch, capsys):
    monkeypatch.setattr(update, "packaged_by", lambda: "AUR|yay -Syu glance-writing-assistant")
    monkeypatch.setattr(update.subprocess, "Popen", _P)
    _P.seen = []
    assert cli.main(["update"]) == update.EXIT_PACKAGED and _P.seen == []
    assert "yay -Syu glance-writing-assistant" in capsys.readouterr().err


def test_installer_command_passes_the_ref():
    assert update.installer_command() == f"curl -fsSL {update.INSTALL_URL} | bash -s -- --ref latest"
    assert update.installer_command("main").endswith("--ref main")
    with pytest.raises(ValueError):
        update.installer_command("v1; rm -rf ~")


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
    env.update_status = lambda: update.UpdateCheck("available", "0.4.0", "0.5.0")
    r = chk.run(env)
    assert r.status == "warn" and "Glance 0.4.0 → 0.5.0" in r.detail
    env.update_status = lambda: 1 / 0
    assert chk.run(env).status == "ok"


def test_update_state_caches():
    st = update.UpdateState(lambda: "available")
    assert st.status == "unknown"
    st.refresh()
    assert st.status == "available"
    st = update.UpdateState(lambda: update.UpdateCheck("available", "0.4.0", B, "main", A))
    st.refresh()
    assert st.update_args() == ["--main"] and st.result.label
    st = update.UpdateState(lambda: update.UpdateCheck("available", "0.4.0", "0.5.0"))
    st.refresh()
    assert st.update_args() == []


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


def test_launch_update_passes_the_channel(tmp_path):
    seen = []

    class P:
        def wait(self):
            return 0

    update.launch_update(lambda s, b: None, tmp_path / "u.log",
                         popen=lambda argv, **k: (seen.append(argv), P())[1], args=["--main"])
    assert seen == [["glance", "update", "--main"]]


def test_launch_update_start_failure(tmp_path):
    notes = []

    def bad(*a, **k):
        raise FileNotFoundError

    assert not update.launch_update(lambda s, b: notes.append(s), tmp_path / "u.log", popen=bad)
    assert notes == ["Glance update failed"]
