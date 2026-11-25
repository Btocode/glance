"""glance doctor: every check against a fake environment, never the real home or desktop."""
from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

from glance.config import GeneralCfg
from glance.doctor import checks as checks_mod
from glance.doctor.checks import CHECKS, CheckResult, DoctorEnv
from glance.doctor.runner import apply_fixes, exit_code, render, run_checks, undo_all
from glance.integrations import claude_wrapper

GRABBED = ",".join(GeneralCfg().hotkeys)  # every default hotkey


class Calls:
    def __init__(self) -> None:
        self.argvs: list[list[str]] = []


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.delenv("GLANCE_SKIP_GSETTINGS", raising=False)
    monkeypatch.delenv("GLANCE_SKIP_SYSTEMCTL", raising=False)
    h = tmp_path / "home"
    (h / ".local/share/glance/bin").mkdir(parents=True)
    harper = h / ".local/share/glance/bin/harper-ls"
    harper.write_bytes(b"fake harper")
    harper.chmod(0o755)
    monkeypatch.setenv("XDG_DATA_HOME", str(h / ".local/share"))
    monkeypatch.setattr(checks_mod, "HARPER_SHA256", hashlib.sha256(b"fake harper").hexdigest())
    (h / ".config/glance").mkdir(parents=True)
    (h / ".config/glance/config.toml").write_text('[backend]\nchoice = "claude"\n')
    claude_wrapper.install(h / ".zshrc")
    return h


def make_env(home: Path, *, calls: Calls | None = None, run=None, **over) -> DoctorEnv:
    calls = calls or Calls()

    def default_run(argv, timeout=None):
        calls.argvs.append(list(argv))
        if argv[0] == "gsettings" and argv[1] == "get":
            return 0, "true\n"
        if argv[0] == "systemctl" and "is-enabled" in argv:
            return 0, "enabled\n"
        if argv[0] == "zsh" and "-ic" in argv:
            return 0, "vim \n"
        if argv[0] == "zsh" and "-lc" in argv:
            return 0, "/home/x/.local/bin/glance-edit\n"
        return 0, ""

    status = {"ok": True, "capture_connected": True, "harper_alive": True,
              "hotkeys_grabbed": GRABBED, "field": {"wm_class": "gedit", "len": 120}}
    fields: dict = dict(  # noqa: C408
        home=home,
        environ={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        run=run or default_run,
        procs=list,
        ctl=lambda t, p: status if t == "status" else None,
        which=lambda name: f"/usr/bin/{name}",
        healths=lambda: {"claude": True, "codex": False},
    )
    fields.update(over)
    return DoctorEnv(**fields)


def by_id(results):
    return {r.id: r for r in results}


def test_all_ok_exit_0(home):
    results = run_checks(make_env(home), CHECKS)
    assert [r.id for r in results] == [c.id for c in CHECKS]
    assert [r.status for r in results if r.status != "ok"] == [], results
    assert exit_code(results) == 0


def test_wayland_session_fails_with_hint(home):
    r = by_id(run_checks(make_env(home, environ={"XDG_SESSION_TYPE": "wayland"}), CHECKS))["session"]
    assert r.status == "fail" and "Xorg" in r.hint


def test_missing_display_fails_session(home):
    r = by_id(run_checks(make_env(home, environ={"XDG_SESSION_TYPE": "x11"}), CHECKS))["session"]
    assert r.status == "fail"


def test_a11y_off_fails_and_fix_sets_gsetting(home):
    calls = Calls()
    state = {"v": "false"}

    def run(argv, timeout=None):
        calls.argvs.append(list(argv))
        if argv[:2] == ["gsettings", "get"]:
            return 0, state["v"] + "\n"
        if argv[:2] == ["gsettings", "set"]:
            state["v"] = argv[-1]
            return 0, ""
        return make_env(home).run(argv)

    env = make_env(home, run=run)
    assert by_id(run_checks(env, CHECKS))["a11y"].status == "fail"
    after = by_id(apply_fixes(env, CHECKS, apps=[]))["a11y"]
    assert after.status == "ok"
    assert ["gsettings", "set", "org.gnome.desktop.interface", "toolkit-accessibility",
            "true"] in calls.argvs


def test_skip_gsettings_env_makes_fix_a_noop(home):
    calls = Calls()
    env = make_env(home, calls=calls,
                   environ={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0",
                            "GLANCE_SKIP_GSETTINGS": "1"})
    check = next(c for c in CHECKS if c.id == "a11y")
    assert check.fix(env) is False
    assert not any(a[:2] == ["gsettings", "set"] for a in calls.argvs)


def test_missing_python_module_is_named(home):
    def run(argv, timeout=None):
        if argv[0] == "/usr/bin/python3" and "Wnck" in " ".join(argv):
            return 1, "Wnck\n"
        return make_env(home).run(argv)

    r = by_id(run_checks(make_env(home, run=run), CHECKS))["python"]
    assert r.status == "fail" and "Wnck" in r.detail


def test_harper_missing_wrong_hash_and_not_executable(home):
    harper = home / ".local/share/glance/bin/harper-ls"
    harper.write_bytes(b"tampered")
    assert by_id(run_checks(make_env(home), CHECKS))["harper"].status == "fail"
    harper.write_bytes(b"fake harper")
    harper.chmod(0o644)
    assert by_id(run_checks(make_env(home), CHECKS))["harper"].status == "fail"
    harper.unlink()
    r = by_id(run_checks(make_env(home), CHECKS))["harper"]
    assert r.status == "fail" and "fetch-harper" in r.hint


def test_harper_fix_runs_fetch_script_with_long_timeout(home):
    seen = []

    def run(argv, timeout=None):
        seen.append((argv, timeout))
        return 0, ""

    (home / ".local/share/glance/bin/harper-ls").unlink()
    next(c for c in CHECKS if c.id == "harper").fix(make_env(home, run=run))
    (argv, timeout), = seen
    assert argv[-1].endswith("fetch-harper.sh") and timeout >= 120


def test_fix_that_raises_is_logged_by_type_and_reported_by_rerun(home, monkeypatch):
    events = []
    monkeypatch.setattr("glance.doctor.runner.log_event", lambda e, **f: events.append((e, f)))
    claude_wrapper.uninstall(home / ".zshrc")
    wrapper = next(c for c in CHECKS if c.id == "wrapper")
    monkeypatch.setattr(wrapper, "fix", lambda env: 1 / 0, raising=False)
    out = by_id(apply_fixes(make_env(home), CHECKS, apps=[]))
    assert out["wrapper"].status == "warn"
    assert events == [("doctor_fix_error", {"check": "wrapper", "error": "ZeroDivisionError"})]


def test_missing_tray_gir_warns_with_apt_hint(home):
    def run(argv, timeout=None):
        if argv[0] == "/usr/bin/python3" and "AyatanaAppIndicator3" in " ".join(argv):
            return 1, "missing"
        return make_env(home).run(argv)

    r = by_id(run_checks(make_env(home, run=run), CHECKS))["tray"]
    assert r.status == "warn"
    assert "sudo apt install gir1.2-ayatanaappindicator3-0.1" in r.hint


def test_bad_config_fails_with_key_and_reason(home):
    (home / ".config/glance/config.toml").write_text("[general]\nchip_idle_ms = 'soon'\n")
    r = by_id(run_checks(make_env(home), CHECKS))["config"]
    assert r.status == "fail" and "chip_idle_ms" in r.detail


def test_daemon_down_is_fail_with_systemctl_hint(home):
    r = by_id(run_checks(make_env(home, ctl=lambda t, p: None), CHECKS))["daemon"]
    assert r.status == "fail" and "not running" in r.detail
    assert "systemctl --user start glance" in r.hint


def test_daemon_capture_and_harper_states(home):
    base = {"ok": True, "hotkeys_grabbed": GRABBED}
    r = by_id(run_checks(make_env(home, ctl=lambda t, p: {**base, "capture_connected": False,
                                                           "harper_alive": True}), CHECKS))["daemon"]
    assert r.status == "fail"
    r = by_id(run_checks(make_env(home, ctl=lambda t, p: {**base, "capture_connected": True,
                                                           "harper_alive": False}), CHECKS))["daemon"]
    assert r.status == "warn"


def test_ungrabbed_hotkey_warns_naming_chord(home):
    ctl = lambda t, p: {"ok": True, "capture_connected": True, "harper_alive": True,
                        "hotkeys_grabbed": "panel,pause,revert"}
    r = by_id(run_checks(make_env(home, ctl=ctl), CHECKS))["hotkeys"]
    assert r.status == "warn" and "Super+Alt+f" in r.detail and "config.toml" in r.hint


def test_no_backend_is_warn_not_fail_and_lists_commands(home):
    (home / ".config/glance/config.toml").write_text("")
    env = make_env(home, healths=lambda: {"claude": False, "codex": False})
    results = run_checks(env, CHECKS)
    r = by_id(results)["backend"]
    assert r.status == "warn" and "No AI connected" in r.detail
    for cmd in ("run `claude` and log in", "codex login", "opencode auth login",
                "glance backend use", "glance backend set-key"):
        assert cmd in (r.detail + (r.hint or "")), cmd
    assert "grammar" in (r.detail + r.hint).lower()
    assert exit_code(results) == 0


def test_backend_health_exception_counts_as_not_connected(home):
    def boom():
        raise RuntimeError("keyring locked")

    r = by_id(run_checks(make_env(home, healths=boom), CHECKS))["backend"]
    assert r.status == "warn" and "No AI connected" in r.detail


def test_running_app_without_flag_is_warn_restart(home):
    e = make_env(home, procs=lambda: [(10, ["/usr/lib/slack/slack"]),
                                      (11, ["/usr/lib/slack/slack", "--type=renderer"])])
    r = by_id(run_checks(e, CHECKS))["running_flags"]
    assert r.status == "warn" and "Restart Slack" in r.detail


def test_running_app_with_flag_is_ok(home):
    e = make_env(home, procs=lambda: [
        (10, ["/usr/lib/slack/slack", "--force-renderer-accessibility"]),
        (11, ["/usr/lib/slack/slack", "--type=renderer"])])
    assert by_id(run_checks(e, CHECKS))["running_flags"].status == "ok"


def test_firefox_snap_missing_connection_warns(home):
    def run(argv, timeout=None):
        if argv[:2] == ["snap", "connections"]:
            return 0, "Interface  Plug  Slot\ndesktop  firefox:desktop  :desktop\n"
        return make_env(home).run(argv)

    procs = lambda: [(5, ["/snap/firefox/9269/usr/lib/firefox/firefox"])]
    r = by_id(run_checks(make_env(home, run=run, procs=procs), CHECKS))["firefox"]
    assert r.status == "warn" and "unity7" in r.detail
    assert "GNOME_ACCESSIBILITY=1" in r.hint


def test_firefox_snap_disconnected_plug_line_is_not_connected(home):
    listing = ("Interface  Plug  Slot  Notes\n"
               "desktop  firefox:desktop  :desktop  -\n"
               "desktop-legacy  firefox:desktop-legacy  :desktop-legacy  -\n"
               "gsettings  firefox:gsettings  :gsettings  -\n"
               "unity7  firefox:unity7  -  -\n")

    def run(argv, timeout=None):
        if argv[:2] == ["snap", "connections"]:
            return 0, listing
        return make_env(home).run(argv)

    procs = lambda: [(5, ["/snap/firefox/9269/usr/lib/firefox/firefox"])]
    r = by_id(run_checks(make_env(home, run=run, procs=procs), CHECKS))["firefox"]
    assert r.status == "warn" and r.detail.endswith("unity7")


def test_firefox_not_running_is_ok(home):
    assert by_id(run_checks(make_env(home), CHECKS))["firefox"].status == "ok"


def test_editor_globally_set_to_glance_edit_is_fail(home):
    def run(argv, timeout=None):
        if argv[0] == "zsh" and "-ic" in argv:
            return 0, "glance-edit \n"
        return make_env(home).run(argv)

    r = by_id(run_checks(make_env(home, run=run), CHECKS))["wrapper"]
    assert r.status == "fail" and "glance-edit" in r.detail


def test_wrapper_absent_warns_and_fix_installs(home):
    claude_wrapper.uninstall(home / ".zshrc")
    env = make_env(home)
    assert by_id(run_checks(env, CHECKS))["wrapper"].status == "warn"
    assert by_id(apply_fixes(env, CHECKS, apps=[]))["wrapper"].status == "ok"
    assert claude_wrapper.status(home / ".zshrc") == "installed"
    undo_all(env, CHECKS)
    assert claude_wrapper.status(home / ".zshrc") == "absent"


def test_wrapper_conflict_is_warn_with_snippet(home):
    z = home / ".zshrc"
    z.write_text("alias claude=claude-old\n")
    r = by_id(run_checks(make_env(home), CHECKS))["wrapper"]
    assert r.status == "warn" and claude_wrapper.BEGIN in r.hint and "alias claude" in r.detail
    assert apply_fixes(make_env(home), CHECKS, apps=[]) and z.read_text() == "alias claude=claude-old\n"


def test_skip_leaves_check_unfixed(home):
    claude_wrapper.uninstall(home / ".zshrc")
    env = make_env(home)
    out = by_id(apply_fixes(env, CHECKS, apps=[], skip=["wrapper"]))
    assert out["wrapper"].status == "warn"
    assert claude_wrapper.status(home / ".zshrc") == "absent"


def test_glance_edit_not_on_path_warns(home):
    def run(argv, timeout=None):
        if argv[0] == "zsh" and "-lc" in argv:
            return 1, ""
        return make_env(home).run(argv)

    r = by_id(run_checks(make_env(home, run=run), CHECKS))["path"]
    assert r.status == "warn" and "PATH" in r.hint


def test_autostart_disabled_warns_and_fix_enables(home):
    calls = Calls()
    state = {"v": "disabled"}

    def run(argv, timeout=None):
        calls.argvs.append(list(argv))
        if argv[:3] == ["systemctl", "--user", "is-enabled"]:
            return 1, state["v"] + "\n"
        if argv[:3] == ["systemctl", "--user", "enable"]:
            state["v"] = "enabled"
            return 0, ""
        return make_env(home).run(argv)

    env = make_env(home, run=run)
    assert by_id(run_checks(env, CHECKS))["autostart"].status == "warn"
    assert by_id(apply_fixes(env, CHECKS, apps=[]))["autostart"].status == "ok"


def test_clipboard_history_extension_noted(home):
    def run(argv, timeout=None):
        if argv[0] == "gnome-extensions":
            return 0, "clipboard-indicator@tudmotu.com\n"
        return make_env(home).run(argv)

    r = by_id(run_checks(make_env(home, run=run), CHECKS))["clipboard_history"]
    assert r.status == "ok" and "password-manager hint" in r.detail


def test_other_electron_is_opt_in_only(home):
    created: list[str] = []
    procs = lambda: [(20, ["/usr/share/code/code"]),
                     (21, ["/usr/share/code/code", "--type=renderer"])]
    env = make_env(home, procs=procs, install_override=lambda app: created.append(app) or True)
    r = by_id(run_checks(env, CHECKS))["other_electron"]
    assert r.status == "warn" and "code" in r.detail and "glance doctor --fix --app" in r.hint
    apply_fixes(env, CHECKS, apps=[])
    assert created == []
    apply_fixes(env, CHECKS, apps=["code"])
    assert created == ["code"]


def test_environment_editor_is_not_read_only_the_login_shell(home):
    env = make_env(home, environ={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0",
                                  "EDITOR": "glance-edit"})
    assert by_id(run_checks(env, CHECKS))["wrapper"].status == "ok"


def test_app_opt_in_works_when_app_is_not_running_and_rejects_bad_ids(home, capsys):
    from glance import cli
    created = []
    env = make_env(home, install_override=lambda a: created.append(a) or True)
    apply_fixes(env, CHECKS, apps=["code", "../evil"])
    assert created == ["code"]
    assert cli.main(["doctor", "--fix", "--app", "a/b"]) == 2


def test_ready_backend_but_none_chosen_does_not_say_no_ai(home):
    (home / ".config/glance/config.toml").write_text("")
    r = by_id(run_checks(make_env(home), CHECKS))["backend"]
    assert r.status == "warn" and "No AI connected" not in r.detail and "claude" in r.hint


def test_checks_are_read_only_by_default(home):
    calls = Calls()
    run_checks(make_env(home, calls=calls), CHECKS)
    verbs = {a[1] for a in calls.argvs if a[0] in ("gsettings", "systemctl")}
    assert verbs <= {"get", "--user"}
    assert not any(a[:3] == ["systemctl", "--user", "enable"] for a in calls.argvs)


def test_a_crashing_check_becomes_a_fail_not_a_traceback(home):
    r = by_id(run_checks(make_env(home, ctl=lambda t, p: 1 / 0), CHECKS))["daemon"]
    assert r.status == "fail" and "ZeroDivisionError" in r.detail


def R(status):
    return CheckResult("x", "Thing", status, "detail", "do this" if status != "ok" else None)


def test_exit_code_and_render():
    assert exit_code([R("ok"), R("warn")]) == 0 and exit_code([R("ok"), R("fail")]) == 1
    assert render([R("fail")]).startswith("✗")
    assert render([R("ok")]).startswith("✓") and render([R("warn")]).startswith("!")
    assert "\n    do this" in render([R("warn")])


def test_doctor_output_has_no_typed_text(home):
    secret = "my private draft sentence"
    ctl = lambda t, p: {"ok": True, "capture_connected": True, "harper_alive": True,
                        "hotkeys_grabbed": GRABBED, "field": {"len": 12, "wm_class": "gedit"},
                        "text": secret}
    results = run_checks(make_env(home, ctl=ctl), CHECKS)
    out = render(results) + json.dumps([dataclasses.asdict(r) for r in results])
    assert secret not in out


def test_cli_doctor_runs_fix_with_skip_and_json(home, monkeypatch, capsys):
    from glance import cli
    from glance.doctor import runner

    claude_wrapper.uninstall(home / ".zshrc")
    monkeypatch.setattr(runner, "default_env", lambda: make_env(home))
    assert cli.main(["doctor", "--fix", "--skip", "wrapper", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert {r["id"]: r["status"] for r in out}["wrapper"] == "warn"
    assert claude_wrapper.status(home / ".zshrc") == "absent"
    assert cli.main(["doctor", "--fix"]) == 0
    assert claude_wrapper.status(home / ".zshrc") == "installed"
    assert cli.main(["doctor", "--undo"]) == 0
    assert claude_wrapper.status(home / ".zshrc") == "absent"
    assert "✓" in capsys.readouterr().out


def test_default_healths_builds_every_backend(monkeypatch):
    """Regression: a wrong class name made default_healths raise, so doctor said
    "No AI connected" while `claude auth status` reported loggedIn."""
    from glance.backends import api, claude, codex, gemini, keystore, opencode
    from glance.backends.base import Health

    signed_in = lambda self: Health(True, True, "signed in")
    for cls in (claude.ClaudeBackend, codex.CodexBackend, gemini.GeminiBackend,
                opencode.OpencodeBackend, api.ApiBackend):
        monkeypatch.setattr(cls, "health", signed_in)
    monkeypatch.setattr(keystore.SecretKeyStore, "__init__", lambda self: None)
    assert checks_mod.default_healths() == dict.fromkeys(
        ("claude", "codex", "gemini", "opencode", "api"), True)


def test_default_healths_one_broken_backend_keeps_the_others(monkeypatch):
    from glance.backends import claude, codex, gemini, keystore, opencode
    from glance.backends.base import Health

    for cls in (claude.ClaudeBackend, codex.CodexBackend, gemini.GeminiBackend,
                opencode.OpencodeBackend):
        monkeypatch.setattr(cls, "health", lambda self: Health(True, True, "signed in"))

    def no_secret(self):
        raise ValueError("no libsecret")

    monkeypatch.setattr(keystore.SecretKeyStore, "__init__", no_secret)
    out = checks_mod.default_healths()
    assert out["claude"] is True and out["api"] is False
