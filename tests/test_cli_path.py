"""The CLI PATH resolver: glanced's service PATH lacks ~/.local/bin, nvm, npm...; the AI CLIs
are found through the login shell's PATH plus the usual install dirs, and spawned by
absolute path with that PATH (node-based CLIs need `node` on it)."""
import ast
import json
import os
import stat
import time
from pathlib import Path

import pytest

from glance.backends import cli_path
from glance.backends.claude import ClaudeBackend
from glance.backends.codex import CodexBackend
from glance.backends.gemini import GeminiBackend
from glance.backends.opencode import OpencodeBackend
from glance.backends.proc import ProcRunner

M = cli_path.MARK
REAL_RUN_SHELL = cli_path.run_shell  # conftest swaps it for a stub in every test


def _exe(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


# -- parsing and the shell's argv ----------------------------------------------------------

def test_parse_takes_the_last_marked_path_and_ignores_rc_noise():
    out = (f"Welcome!\n{M}/fake/early{M}\nnvm: using node 20\n"
           f"{M}/home/u/.local/bin:/usr/bin:relative/dir::/bin{M}\nbye\n")
    assert cli_path.parse(out) == ["/home/u/.local/bin", "/usr/bin", "/bin"]


@pytest.mark.parametrize("out", [None, "", "no markers here", f"{M} unterminated"])
def test_parse_without_a_marked_path_is_empty(out):
    assert cli_path.parse(out) == []


@pytest.mark.parametrize("shell", ["/bin/zsh", "/usr/bin/bash", "/bin/sh"])
def test_posix_shells_run_login_interactive_with_a_constant_script(shell):
    argv = cli_path.shell_argv(shell)
    assert argv[:4] == [shell, "-l", "-i", "-c"] and len(argv) == 5
    assert '"$PATH"' in argv[4] and argv[4].count(M) == 2


def test_fish_joins_its_path_list_and_csh_is_skipped():
    argv = cli_path.shell_argv("/usr/bin/fish")
    assert argv[:4] == ["/usr/bin/fish", "-l", "-i", "-c"] and "string join :" in argv[4]
    assert cli_path.shell_argv("/bin/tcsh") is None


def test_the_probe_really_prints_path_between_markers():
    out = REAL_RUN_SHELL(["/bin/sh", "-c", cli_path._SCRIPT], 3.0)
    assert cli_path.parse(out) == [d for d in os.environ["PATH"].split(":")
                                   if d and os.path.isabs(d)]


def test_a_stuck_shell_is_killed_at_the_timeout_even_with_a_child_holding_the_pipe():
    t = time.monotonic()
    assert REAL_RUN_SHELL(["/bin/sh", "-c", "sleep 30 & sleep 30"], 0.3) is None
    assert time.monotonic() - t < 2.0


def test_login_shell_is_shell_else_the_passwd_entry(monkeypatch):
    assert cli_path.login_shell({"SHELL": "/bin/sh"}) == "/bin/sh"
    assert cli_path.login_shell({"SHELL": "/nonexistent/zsh"}) is None
    monkeypatch.setattr(cli_path.pwd, "getpwuid",
                        lambda _uid: type("P", (), {"pw_shell": "/bin/sh"})())
    assert cli_path.login_shell({}) == "/bin/sh"


# -- merging ---------------------------------------------------------------------------------

def test_well_known_dirs_that_exist_and_the_newest_nvm_node(tmp_path):
    for d in (".local/bin", ".bun/bin", ".claude/local", "go/bin"):
        (tmp_path / d).mkdir(parents=True)
    for v in ("v9.11.2", "v20.11.1", "v18.19.0", "v20.9.0"):
        (tmp_path / ".nvm/versions/node" / v / "bin").mkdir(parents=True)
    (tmp_path / ".nvm/versions/node/v99.0.0").mkdir()  # no bin: not a usable node
    dirs = cli_path.well_known(tmp_path)
    home = [d for d in dirs if d.startswith(str(tmp_path))]
    assert home == [str(tmp_path / d) for d in (".local/bin", ".claude/local", ".bun/bin",
                                                "go/bin", ".nvm/versions/node/v20.11.1/bin")]
    assert all(os.path.isdir(d) for d in dirs)


def test_resolve_puts_the_shells_path_first_then_the_current_then_the_usual(tmp_path):
    (tmp_path / ".local/bin").mkdir(parents=True)
    seen = []

    def runner(argv, timeout_s):
        seen.append((argv, timeout_s))
        return f"rc noise\n{M}/opt/shell/bin:/usr/bin{M}\n"

    path = cli_path.resolve({"SHELL": "/bin/sh", "PATH": "/usr/bin:/service/bin"},
                            tmp_path, runner)
    parts = path.split(":")
    assert parts[:4] == ["/opt/shell/bin", "/usr/bin", "/service/bin", str(tmp_path / ".local/bin")]
    assert len(parts) == len(set(parts))
    assert seen == [(cli_path.shell_argv("/bin/sh"), cli_path.SHELL_TIMEOUT_S)]


def test_a_failed_or_timed_out_shell_still_gets_the_usual_dirs(tmp_path):
    (tmp_path / ".local/bin").mkdir(parents=True)
    path = cli_path.resolve({"SHELL": "/bin/sh", "PATH": "/usr/bin"}, tmp_path,
                            lambda argv, t: None)
    assert path.split(":")[:2] == ["/usr/bin", str(tmp_path / ".local/bin")]


def test_the_log_says_how_many_dirs_never_which(tmp_path, capsys):
    secret = "/home/someone/very-private-dir"
    cli_path.resolve({"SHELL": "/bin/sh", "PATH": "/usr/bin"}, tmp_path,
                     lambda argv, t: f"{M}{secret}{M}")
    err = capsys.readouterr().err
    assert "cli_path_resolved" in err and "shell_dirs=1" in err and secret not in err


def test_resolved_once_then_cached_until_refresh(monkeypatch):
    calls = []
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setattr(cli_path, "run_shell",
                        lambda argv, t: calls.append(1) or f"{M}/opt/a{M}")
    cli_path.refresh()
    assert cli_path.search_path().startswith("/opt/a:")
    cli_path.search_path()
    cli_path.find("sh")
    assert len(calls) == 1
    cli_path.refresh()
    cli_path.search_path()
    assert len(calls) == 2


def test_find_and_where(tmp_path, monkeypatch):
    claude = _exe(tmp_path / "bin" / "claude", "exit 0\n")
    (tmp_path / "bin" / "notexec").write_text("")
    monkeypatch.setattr(cli_path, "_cache", [str(tmp_path / "bin")])
    assert cli_path.find("claude") == str(claude)
    assert cli_path.find(str(claude)) == str(claude)
    assert cli_path.find("notexec") is None and cli_path.find("codex") is None
    assert cli_path.where("claude") == f"found at {claude}"
    assert cli_path.where("codex") == "not found on your PATH or in the usual places"


# -- backends spawn the resolved path with the merged PATH --------------------------------

@pytest.fixture
def userbin(tmp_path, monkeypatch):
    """A dir only the resolver knows (not in this process's PATH), like ~/.local/bin under
    systemd; the merged PATH is that dir plus the system's."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    bin_dir = tmp_path / "userbin"
    bin_dir.mkdir()
    merged = f"{bin_dir}:/usr/bin:/bin"
    monkeypatch.setattr(cli_path, "_cache", [merged])
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    return bin_dir, merged, tmp_path / "rec.json"


def _recorder(bin_dir: Path, name: str, rec: Path, then: str) -> None:
    _exe(bin_dir / name, f"printf '{{\"argv0\": \"%s\", \"path\": \"%s\"}}' \"$0\" \"$PATH\" "
                         f"> '{rec}'\n{then}")


@pytest.mark.parametrize("make, then", [
    (lambda: ClaudeBackend(ProcRunner()), "echo '{\"loggedIn\": true}'\n"),
    (lambda: CodexBackend(ProcRunner()), "echo 'Logged in using ChatGPT'\n"),
    (lambda: OpencodeBackend(ProcRunner()), "echo '1 credentials'\n"),
], ids=["claude", "codex", "opencode"])
def test_cli_backends_run_the_resolved_absolute_path_with_the_merged_path(userbin, make, then):
    bin_dir, merged, rec = userbin
    b = make()
    _recorder(bin_dir, b.binary, rec, then)
    h = b.health()
    assert h.installed and h.signed_in is True, h
    got = json.loads(rec.read_text())
    assert got == {"argv0": str(bin_dir / b.binary), "path": merged}


def test_gemini_resolves_through_the_shared_resolver(userbin):
    bin_dir, _merged, rec = userbin
    b = GeminiBackend(ProcRunner())
    assert not b.health().installed
    _recorder(bin_dir, "gemini", rec, "")
    assert b.health().installed
    assert b._argv("json")[0] == str(bin_dir / "gemini")


def test_opencode_server_is_spawned_by_absolute_path_with_the_merged_path(userbin):
    bin_dir, merged, rec = userbin
    _recorder(bin_dir, "opencode", rec,
              "echo 'opencode server listening on http://127.0.0.1:4096'\nexec sleep 30\n")
    b = OpencodeBackend(ProcRunner())
    try:
        assert b.ensure_server() == "http://127.0.0.1:4096"
        for _ in range(100):
            if rec.exists() and rec.read_text():
                break
            time.sleep(0.02)
        assert json.loads(rec.read_text()) == {"argv0": str(bin_dir / "opencode"),
                                               "path": merged}
    finally:
        b.stop()


def test_a_cli_found_nowhere_is_not_installed(userbin):
    for b in (ClaudeBackend(ProcRunner()), CodexBackend(ProcRunner()),
              OpencodeBackend(ProcRunner()), GeminiBackend(ProcRunner())):
        assert b.health().installed is False, b.name


def test_no_backend_looks_binaries_up_on_its_own():
    """Every CLI lookup goes through cli_path: no shutil.which / login shell of its own."""
    src = Path(cli_path.__file__).parent
    for f in ("claude.py", "codex.py", "gemini.py", "opencode.py", "proc.py"):
        text = (src / f).read_text()
        assert "shutil.which" not in text and "-lic" not in text, f


# -- doctor and `glance backend list` ------------------------------------------------------

def test_doctor_lists_where_each_cli_is(tmp_path):
    from glance.doctor.checks import CHECKS, AiClis
    from glance.doctor.runner import render
    from tests.test_doctor import make_env

    assert any(isinstance(c, AiClis) for c in CHECKS)
    env = make_env(tmp_path, locate=lambda n: "/home/u/.local/bin/claude"
                   if n == "claude" else None)
    r = AiClis().run(env)
    assert r.status == "ok" and r.detail == "1 of 4 found"
    text = render([r])
    assert "claude: /home/u/.local/bin/claude" in text
    for n in ("codex", "gemini", "opencode"):
        assert f"{n}: not found on your PATH or in the usual places" in text


def test_backend_list_prints_where_glanced_found_each_cli(monkeypatch, capsys):
    from glance import cli

    reply = {"ok": True, "choice": None, "models": {},
             "where": {"claude": "/home/u/.local/bin/claude", "codex": None,
                       "gemini": None, "opencode": None},
             "backends": {n: {"installed": n == "claude", "signed_in": False, "detail": ""}
                          for n in ("claude", "codex", "gemini", "opencode", "api")}}
    monkeypatch.setattr(cli, "_ask", lambda *a, **k: (reply, 0))
    assert cli._backend_list() == 0
    out = capsys.readouterr().out
    assert "found at /home/u/.local/bin/claude" in out
    assert out.count("not found on your PATH or in the usual places") == 3


# -- no surprise windows -------------------------------------------------------------------

def _main_tree():
    import glance.daemon.main as m
    return ast.parse(Path(m.__file__).read_text())


def test_x11_gtk_adapters_are_only_created_outside_the_wayland_branch():
    """On GNOME Wayland (DISPLAY set by XWayland or not) glanced never imports or builds the
    GTK overlay / ghost / Xlib hotkeys: they would be ordinary toplevel windows there."""
    x11 = {"glance.ui.overlay_gtk", "glance.ui.ghost_gtk", "glance.ui.hotkeys_xlib",
           "glance.ui.selection_xlib"}
    tree = _main_tree()
    branch = next(n for n in ast.walk(tree) if isinstance(n, ast.If)
                  and isinstance(n.test, ast.Name) and n.test.id == "wayland")

    def imports(nodes):
        return {n.module for node in nodes for n in ast.walk(node)
                if isinstance(n, ast.ImportFrom)}

    assert not imports(branch.body) & x11
    assert imports(branch.orelse) >= x11
    everywhere = imports([tree])
    assert everywhere & x11 == imports(branch.orelse) & x11  # nowhere else either
    names = {n.attr if isinstance(n, ast.Attribute) else n.id for node in branch.body
             for n in ast.walk(node) if isinstance(n, (ast.Name, ast.Attribute))}
    assert "ExtensionNotice" in names and "GtkOverlay" not in names


def test_wayland_with_xwayland_display_is_still_the_wayland_session():
    from glance.session import GNOME_WAYLAND, session_kind

    env = {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0",
           "XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}
    assert session_kind(env) == GNOME_WAYLAND


def test_missing_extension_gets_one_rate_limited_notification(tmp_path):
    from glance.daemon.wiring import EXTENSION_NOTICE, EXTENSION_NOTICE_EVERY_S, ExtensionNotice

    sent, now = [], [1_000_000.0]
    stamp = tmp_path / "state" / "extension-notice"

    def start():
        return ExtensionNotice(stamp, lambda *m: sent.append(m), lambda: now[0])

    n = start()
    assert n.check(True) is False and sent == []  # the extension is up: nothing
    assert n.check(False) is True and sent == [EXTENSION_NOTICE]
    assert n.check(False) is False  # once per run
    assert EXTENSION_NOTICE[0] == "Log out and back in once to finish setting up Glance"
    now[0] += 60
    assert start().check(False) is False  # a restart a minute later: still quiet
    now[0] += EXTENSION_NOTICE_EVERY_S
    assert start().check(False) is True and len(sent) == 2


def test_extension_notice_survives_an_unwritable_state_dir(tmp_path):
    from glance.daemon.wiring import ExtensionNotice

    blocker = tmp_path / "file"
    blocker.write_text("")
    sent = []
    assert ExtensionNotice(blocker / "sub" / "stamp", lambda *m: sent.append(m)).check(False)
    assert len(sent) == 1

