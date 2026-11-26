"""First-run fixes: macOS Accessibility prompt + polling, quiet CLI logs, the auto-chosen AI.

All with fakes: no PyObjC, no display, no real HOME, no AI calls.
"""
from __future__ import annotations

import re
import threading

import pytest

from glance import cli
from glance.backends import auto_choice
from glance.backends.base import Health
from glance.config import BackendCfg, load_config
from glance.doctor.checks import Backend, DoctorEnv
from glance.log import log_event, quiet
from glance.platform.macos import ax_trust
from glance.platform.macos import doctor as md
from tests.test_supervisor_backend import SIGNED_OUT, Rig, StubBackend

LOG_LINE = re.compile(r"^\S+( \w+=\S*)+$", re.MULTILINE)
EXE = "/Users/x/.local/share/glance/python/cpython-3.12.16-macos-aarch64-none/bin/python3.12"


# ---------------------------------------------------------------- Accessibility (glanced)
class FakeTimer:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class AxRig:
    def __init__(self, tmp_path, trusted=False, exe=EXE):
        self.trusted = trusted
        self.prompts = 0
        self.timers: list[tuple[int, object, FakeTimer]] = []
        self.restarts = 0
        self.state = tmp_path / "state" / ax_trust.STATE_FILE
        self.exe = exe

    def every(self, ms, fn):
        t = FakeTimer()
        self.timers.append((ms, fn, t))
        return t

    def prompt(self):
        self.prompts += 1

    def watch(self):
        return ax_trust.AxTrustWatch(lambda: self.trusted, self.prompt, self.every, self.exe,
                                     self.state, self._restart)

    def _restart(self):
        self.restarts += 1

    def tick(self):
        for _ms, fn, t in self.timers:
            if not t.cancelled:
                fn()


def test_untrusted_start_prompts_once_per_binary_and_polls(tmp_path):
    r = AxRig(tmp_path)
    w = r.watch()
    assert w.start() is False
    assert r.prompts == 1 and w.prompted and w.waiting
    assert r.timers[0][0] == ax_trust.POLL_MS == 2000
    assert ax_trust.prompted_paths(r.state) == {EXE}
    # the next start of the same binary: no second dialog, still polling
    w2 = r.watch()
    assert w2.start() is False and r.prompts == 1 and w2.prompted and w2.waiting
    # a new Python (an update moved the binary): asked again
    r.exe = EXE.replace("3.12.16", "3.12.17")
    assert r.watch().start() is False and r.prompts == 2


def test_polling_notices_the_switch_and_restarts_capture_once(tmp_path):
    r = AxRig(tmp_path)
    w = r.watch()
    w.start()
    r.tick()
    r.tick()
    assert r.restarts == 0 and not w.trusted
    r.trusted = True
    r.tick()
    assert r.restarts == 1 and w.trusted and not w.waiting
    assert r.timers[0][2].cancelled
    r.tick()
    assert r.restarts == 1


def test_already_trusted_never_prompts_or_polls(tmp_path):
    r = AxRig(tmp_path, trusted=True)
    w = r.watch()
    assert w.start() is True
    assert r.prompts == 0 and r.timers == [] and not r.state.exists() and not w.waiting


def test_a_failing_prompt_is_not_remembered(tmp_path):
    r = AxRig(tmp_path)

    def boom():
        raise RuntimeError

    w = ax_trust.AxTrustWatch(lambda: False, boom, r.every, EXE, r.state, lambda: None)
    assert w.start() is False and not w.prompted and w.waiting
    assert ax_trust.prompted_paths(r.state) == set()


def test_open_pane_command():
    assert ax_trust.open_pane_argv() == [
        "open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"]


# ---------------------------------------------------------------- Accessibility (doctor)
def mac_env(tmp_path, status, open_rc=0):
    calls: list[list[str]] = []

    def run(argv, timeout=None):
        calls.append(list(argv))
        return (open_rc, "") if argv[:1] == ["open"] else (1, "")

    def ctl(msg, payload):
        calls.append(["ctl", msg, *(["prompt"] if payload.get("prompt") else [])])
        return status if msg == "status" else {"ok": True}

    e = DoctorEnv(home=tmp_path, environ={"GLANCE_MACOS_VERSION": "15.6",
                                          "GLANCE_MACOS_ARCH": "arm64"},
                  run=run, procs=list, ctl=ctl, which=lambda n: None)
    return e, calls


def test_doctor_says_what_to_switch_on(tmp_path):
    e, calls = mac_env(tmp_path, {"ax_trusted": False, "glanced_exe": EXE, "ax_waiting": True,
                                  "ax_prompted": True})
    r = md.Accessibility().run(e)
    assert r.status == "fail"
    assert r.detail == ("Glance asked macOS for access: switch on python3.12 under "
                        "Privacy & Security → Accessibility")
    assert EXE in r.hint and "glance doctor --fix" in r.hint
    assert "launchctl" not in r.hint  # glanced notices the switch by itself
    assert not any(c[:1] == ["open"] for c in calls)  # plain doctor opens nothing


def test_doctor_keeps_the_kickstart_line_for_a_glanced_that_does_not_poll(tmp_path):
    e, _ = mac_env(tmp_path, {"ax_trusted": False, "glanced_exe": EXE})
    assert "launchctl kickstart -k" in md.Accessibility().run(e).hint


def test_doctor_fix_opens_the_pane_and_says_so(tmp_path):
    st = {"ax_trusted": False, "glanced_exe": EXE, "ax_waiting": True, "ax_prompted": True}
    e, calls = mac_env(tmp_path, st)
    check = md.Accessibility()
    assert check.fix(e) is False  # only the user can switch it on
    assert ax_trust.open_pane_argv() in calls
    assert ["ctl", "ax_prompt", "prompt"] not in calls  # glanced already asked macOS
    r = check.run(e)
    assert r.detail == ("Glance asked macOS for access: switch on python3.12 under "
                        "Privacy & Security → Accessibility (opened for you)")
    assert EXE in r.hint and "--fix" not in r.hint


def test_doctor_fix_asks_glanced_for_the_dialog_when_it_never_did(tmp_path):
    e, calls = mac_env(tmp_path, {"ax_trusted": False, "ax_prompted": False})
    md.Accessibility().fix(e)
    assert ["ctl", "ax_prompt", "prompt"] in calls


def test_doctor_while_capture_restarts_after_the_grant(tmp_path):
    e, _ = mac_env(tmp_path, {"ax_trusted": True, "capture_ax_trusted": False,
                              "glanced_exe": EXE})
    assert md.Accessibility().run(e).status == "warn"


# ---------------------------------------------------------------- quiet CLI
@pytest.fixture
def cli_home(tmp_path, monkeypatch):
    for var, sub in (("HOME", "home"), ("XDG_RUNTIME_DIR", "run"), ("XDG_CONFIG_HOME", "cfg"),
                     ("XDG_DATA_HOME", "data"), ("XDG_STATE_HOME", "state")):
        (tmp_path / sub).mkdir()
        monkeypatch.setenv(var, str(tmp_path / sub))
    monkeypatch.delenv("GLANCE_DEBUG_LOG", raising=False)
    return tmp_path


def _logs_from_a_thread() -> None:
    log_event("cli_path_resolved", shell="zsh", shell_dirs=14, dirs=13)
    t = threading.Thread(target=log_event, args=("health",), kwargs={"backend": "claude"})
    t.start()
    t.join()


@pytest.mark.parametrize("argv", [["doctor"], ["doctor", "--json"], ["status"],
                                  ["backend", "list"], ["usage"], ["update", "--check"]])
def test_cli_commands_print_no_log_lines(cli_home, monkeypatch, capsys, argv):
    from glance import update
    from glance.doctor import runner

    def request(*_a, **_k):
        _logs_from_a_thread()
        # None: glanced is not running

    def checks(env, checks, on_result=None, on_start=None):
        _logs_from_a_thread()
        return []

    monkeypatch.setattr(cli, "_request", request)
    monkeypatch.setattr(runner, "run_checks", checks)
    monkeypatch.setattr(update, "check", lambda: (_logs_from_a_thread(), ("current", "a", "a"))[1])
    cli.main(argv)
    out = capsys.readouterr()
    for stream in (out.out, out.err):
        assert "cli_path_resolved" not in stream and "backend=claude" not in stream
        assert not LOG_LINE.search(stream), stream


def test_glanced_keeps_logging_and_quiet_is_undone(capsys, monkeypatch):
    monkeypatch.delenv("GLANCE_DEBUG_LOG", raising=False)
    with quiet():
        log_event("hidden", n=1)
    log_event("shown", n=2)
    assert capsys.readouterr().err == "shown n=2\n"
    monkeypatch.setenv("GLANCE_DEBUG_LOG", "1")
    with quiet():
        log_event("debug", n=3)
    assert capsys.readouterr().err == "debug n=3\n"


# ---------------------------------------------------------------- auto-chosen AI
EXPERIMENTAL = Health(True, True, "signed in (experimental: no verified reply)")


def test_pick_order_and_experimental_rule():
    assert auto_choice.pick(["api", "gemini", "claude"]) == "claude"
    assert auto_choice.pick(["api", "gemini"]) == "gemini"
    assert auto_choice.pick(["codex", "api"]) == "api"  # never experimental over stable
    assert auto_choice.pick(["opencode", "codex"]) == "codex"  # only experimental: first
    assert auto_choice.pick([]) is None
    healths = {"claude": SIGNED_OUT, "codex": EXPERIMENTAL, "gemini": Health(True, None, "?"),
               "opencode": Health(False, None, "x"), "api": Health(True, True, "key")}
    assert auto_choice.pick_from(healths) == "api"


def test_glanced_writes_the_choice_once_when_there_was_none(tmp_path):
    r = Rig(tmp_path, BackendCfg(choice=None))
    r.config_path.write_text("# mine\n")
    assert r.sup.active() is None
    assert r.sup.auto_choose() == "claude"
    assert load_config(r.config_path).backend.choice == "claude"
    assert r.config_path.read_text().startswith("# mine\n")
    assert auto_choice.read_marker(r.config_path) == "claude"
    assert r.sup.active() == "claude"  # before the config reloads
    assert r.sup.auto_choose() is None  # said once
    r.cfg = BackendCfg(choice="claude")  # the reload
    assert r.sup.auto_choose() is None and r.sup.active() == "claude"


def test_a_broken_choice_is_stood_in_for_without_touching_the_config(tmp_path):
    backends = {n: StubBackend(n) for n in ("claude", "codex", "gemini")}
    backends["gemini"].health_value = SIGNED_OUT
    backends["claude"].health_value = SIGNED_OUT
    backends["codex"].health_value = EXPERIMENTAL
    r = Rig(tmp_path, BackendCfg(choice="gemini"), backends=backends)
    assert r.sup.auto_choose() == "codex"  # experimental, but the only one connected
    assert not r.config_path.exists() and auto_choice.read_marker(r.config_path) is None
    assert r.sup.active() == "codex"
    backends["claude"].health_value = Health(True, True, "signed in")
    r.sup.healths(True)
    assert r.sup.auto_choose() == "claude"  # a stable one appeared
    backends["gemini"].health_value = Health(True, True, "signed in")
    r.sup.healths(True)
    assert r.sup.auto_choose() is None and r.sup.auto_chosen is None
    assert r.sup.active() == "gemini"


def test_nothing_connected_picks_nothing(tmp_path):
    backends = {"claude": StubBackend("claude", health=SIGNED_OUT)}
    r = Rig(tmp_path, BackendCfg(choice=None), backends=backends)
    assert r.sup.auto_choose() is None and not r.config_path.exists()


def test_the_users_own_choice_clears_the_marker(tmp_path):
    r = Rig(tmp_path, BackendCfg(choice=None))
    r.sup.auto_choose()
    r.sup.use("gemini")
    assert auto_choice.read_marker(r.config_path) is None


def backend_env(tmp_path, healths, config=""):
    cfg = tmp_path / ".config" / "glance" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(config)
    return DoctorEnv(home=tmp_path, environ={}, run=lambda *a, **k: (1, ""), procs=list,
                     ctl=lambda *a: None, which=lambda n: None, healths=lambda: healths), cfg


def test_doctor_backend_lines(tmp_path):
    e, cfg = backend_env(tmp_path, {"claude": True, "codex": True, "api": False})
    r = Backend().run(e)  # no choice yet: glanced picks claude at start
    assert (r.status, r.detail) == ("ok", "claude (chosen automatically)")
    cfg.write_text('[backend]\nchoice = "claude"\n')
    auto_choice.write_marker(cfg, "claude")
    assert Backend().run(e).detail == "claude (chosen automatically)"
    auto_choice.write_marker(cfg, None)
    assert Backend().run(e).detail == "claude connected"
    e, cfg = backend_env(tmp_path, {"claude": False, "codex": True, "api": True},
                         '[backend]\nchoice = "claude"\n')
    r = Backend().run(e)
    assert r.status == "ok" and r.detail == "api (chosen automatically; claude is not connected)"
    e, _ = backend_env(tmp_path, {"claude": False, "codex": False}, "")
    assert Backend().run(e).status == "warn"
