"""glance-edit: the $EDITOR shim for Claude Code's Ctrl+G (findings S7.1, S7.2).

Its whole protocol with Claude Code is the exit code and the file: exit 0 with a changed
file applies, exit 0 with the file untouched cancels, a non-zero exit keeps the prompt.
Whenever anything is not right it becomes the user's real editor, with the same argv.
"""
from __future__ import annotations

import os
import time
from unittest import mock

import pytest

from glance.edit_shim import main as shim
from glance.ipc import JsonLineServer

PROMPT = "claude-prompt-0f3c9a1e-7b2d-4c5e-9a8b-1d2e3f4a5b6c.md"
CLAUDE_ENV = {"CLAUDE_CODE_ENTRYPOINT": "cli", "GLANCE_REAL_EDITOR": "vim -u NONE"}


class Execd(BaseException):
    """What the exec spy raises: a real exec never returns, and nothing may catch it."""


class ExecSpy:
    def __init__(self) -> None:
        self.argv: list[str] | None = None
        self.file_at_exec: str | None = None

        self.env: dict | None = None

    def __call__(self, file: str, argv: list[str], env: dict) -> None:
        assert file == argv[0]
        self.argv, self.env = list(argv), dict(env)
        raise Execd


@pytest.fixture
def exec_spy(monkeypatch):
    spy = ExecSpy()
    monkeypatch.setattr(shim, "_execvpe", spy)
    return spy


@pytest.fixture
def runtime(tmp_path):
    d = tmp_path / "rt"
    d.mkdir(mode=0o700)
    return d


class FakeCtl:
    """A stand-in glanced on `runtime/glance/ctl.sock`: answers ping, and edit_session with
    `reply` (or drops the connection when `reply` is None)."""

    def __init__(self, runtime) -> None:
        self.reply: dict | None = {"action": "cancel"}
        self.received: list[tuple[str, dict]] = []
        self.server = JsonLineServer(runtime / "glance" / "ctl.sock", self._on)

    def _on(self, conn, msg_type, payload) -> None:
        self.received.append((msg_type, payload))
        if msg_type == "ping":
            conn.send(msg_type, {"ok": True, "pid": 1, "id": payload["id"]})
            return
        if self.reply is None:
            conn.close()
            return
        conn.send(msg_type, {**self.reply, "id": payload["id"]})


@pytest.fixture
def fake_ctl(runtime, exec_spy):
    ctl = FakeCtl(runtime)
    ctl.server.start()
    yield ctl
    ctl.server.stop()


def main_with(env, ancestors, argv, monkeypatch=None):
    """Run the shim with exactly `env` and the given ancestor chain. Returns its exit code,
    or "exec" when it exec'd the real editor."""
    with mock.patch.dict(os.environ, env, clear=True), \
            mock.patch.object(shim, "ancestors", lambda: list(ancestors)):
        try:
            return shim.main(argv)
        except Execd:
            return "exec"


def claude_run(runtime, argv, **extra):
    env = {**CLAUDE_ENV, "XDG_RUNTIME_DIR": str(runtime), **extra}
    return main_with(env, ["zsh"], argv)


# ---------------------------------------------------------------- who is calling


def test_git_commit_editmsg_is_not_claude():
    env = {"CLAUDE_CODE_ENTRYPOINT": "cli"}  # inherited, as in Claude's own Bash tool
    assert not shim.is_claude_code(env, ["git", "claude"], "/repo/.git/COMMIT_EDITMSG")


def test_claude_prompt_path_with_entrypoint_is_claude():
    assert shim.is_claude_code({"CLAUDE_CODE_ENTRYPOINT": "cli"}, ["ghostty"],
                               f"/tmp/claude-1000/{PROMPT}")


def test_claude_prompt_path_with_claude_ancestor_is_claude():
    assert shim.is_claude_code({}, ["claude", "ghostty", "systemd"], PROMPT)


def test_claudecode_env_alone_is_not_enough():
    assert not shim.is_claude_code({"CLAUDECODE": "1"}, ["zsh"], PROMPT)


def test_prompt_name_must_match_exactly():
    env = {"CLAUDE_CODE_ENTRYPOINT": "cli"}
    for name in ("claude-prompt-XYZ.md", "claude-prompt-abc.txt", "x-claude-prompt-ab.md",
                 "claude-prompt-.md"):
        assert not shim.is_claude_code(env, ["claude"], f"/tmp/{name}"), name


def test_ancestors_reads_proc_upwards():
    chain = shim.ancestors()
    assert 1 <= len(chain) <= 8 and all(isinstance(c, str) and c for c in chain)


# ---------------------------------------------------------------- the real editor


def test_real_editor_never_resolves_to_itself():
    assert shim.real_editor({"EDITOR": "glance-edit", "VISUAL": "glance-edit"})[0] in (
        "sensible-editor", "nano", "vi")


def test_real_editor_order_and_split():
    env = {"GLANCE_REAL_EDITOR": "", "VISUAL": "/home/u/.local/bin/glance-edit --x",
           "EDITOR": "code --wait"}
    assert shim.real_editor(env) == ["code", "--wait"]
    assert shim.real_editor({"GLANCE_REAL_EDITOR": "emacs -nw", "VISUAL": "vim"}) == \
        ["emacs", "-nw"]


def test_passthrough_when_not_claude_code(exec_spy, tmp_path):
    f = tmp_path / "COMMIT_EDITMSG"
    f.write_text("msg")
    assert main_with({"GLANCE_REAL_EDITOR": "vim -u NONE"}, ["zsh", "git"], [str(f)]) == "exec"
    assert exec_spy.argv == ["vim", "-u", "NONE", str(f)] and f.read_text() == "msg"


def test_passthrough_keeps_flags(exec_spy, tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("x")
    main_with({"EDITOR": "nano"}, ["zsh"], ["+3", str(f)])
    assert exec_spy.argv == ["nano", "+3", str(f)]


def test_multiple_file_args_pass_through(exec_spy, tmp_path, runtime):
    a, b = tmp_path / PROMPT, tmp_path / "b.md"
    a.write_text("one")
    b.write_text("two")
    assert claude_run(runtime, [str(a), str(b)]) == "exec"
    assert exec_spy.argv == ["vim", "-u", "NONE", str(a), str(b)]


def test_no_file_argument_passes_through(exec_spy, runtime):
    assert claude_run(runtime, []) == "exec" and exec_spy.argv == ["vim", "-u", "NONE"]


def test_force_claude_only_with_glance_test():
    env = {"GLANCE_TEST_FORCE_CLAUDE": "1"}
    assert shim._forced(env) is False and shim._forced({**env, "GLANCE_TEST": "1"}) is True


# ---------------------------------------------------------------- daemon trouble


def test_falls_through_when_daemon_down(exec_spy, tmp_path, runtime):
    f = tmp_path / PROMPT
    f.write_text("fix teh bug")
    t0 = time.monotonic()
    assert claude_run(runtime, [str(f)]) == "exec"
    assert time.monotonic() - t0 < 1.0
    assert exec_spy.argv == ["vim", "-u", "NONE", str(f)] and f.read_text() == "fix teh bug"


def test_falls_through_when_daemon_dies_mid_session(exec_spy, fake_ctl, tmp_path, runtime):
    f = tmp_path / PROMPT
    f.write_text("fix teh bug")
    fake_ctl.reply = None  # accepts edit_session, then closes the connection
    assert claude_run(runtime, [str(f)]) == "exec"
    assert [t for t, _ in fake_ctl.received] == ["ping", "edit_session"]
    assert exec_spy.argv == ["vim", "-u", "NONE", str(f)] and f.read_text() == "fix teh bug"


def test_falls_through_when_daemon_does_not_answer(exec_spy, tmp_path, runtime):
    """A daemon whose main loop is stuck accepts the connection but never replies."""
    import socket

    path = runtime / "glance" / "ctl.sock"
    path.parent.mkdir()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    f = tmp_path / PROMPT
    f.write_text("fix teh bug")
    try:
        t0 = time.monotonic()
        assert claude_run(runtime, [str(f)]) == "exec"
        assert time.monotonic() - t0 < 3.0
    finally:
        listener.close()
    assert f.read_text() == "fix teh bug"


def test_sends_prompt_and_window(fake_ctl, tmp_path, runtime):
    f = tmp_path / PROMPT
    f.write_text("fix teh bug\n")
    assert claude_run(runtime, [str(f)], WINDOWID="4194307") == 0
    (_, payload), = [m for m in fake_ctl.received if m[0] == "edit_session"]
    assert payload["text"] == "fix teh bug" and payload["window"] == 4194307


def test_apply_writes_file_and_returns_0(fake_ctl, tmp_path, runtime):
    f = tmp_path / PROMPT
    f.write_text("fix teh bug\n")
    fake_ctl.reply = {"action": "apply", "text": "Fix the bug in [which file?]"}
    assert claude_run(runtime, [str(f)]) == 0
    assert f.read_text() == "Fix the bug in [which file?]\n"  # trailing newline preserved


def test_apply_never_adds_a_trailing_newline(fake_ctl, tmp_path, runtime):
    f = tmp_path / PROMPT
    f.write_text("fix teh bug")
    fake_ctl.reply = {"action": "apply", "text": "Fix the bug.\n"}
    assert claude_run(runtime, [str(f)]) == 0
    assert f.read_text() == "Fix the bug."


def test_apply_keeps_mode_and_leaves_no_temp_file(fake_ctl, tmp_path, runtime):
    f = tmp_path / PROMPT
    f.write_text("fix teh bug")
    f.chmod(0o600)
    fake_ctl.reply = {"action": "apply", "text": "Fix the bug."}
    assert claude_run(runtime, [str(f)]) == 0
    assert oct(f.stat().st_mode & 0o777) == "0o600"
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(["rt", PROMPT])


def test_cancel_leaves_file_untouched(fake_ctl, tmp_path, runtime):
    f = tmp_path / PROMPT
    f.write_text("fix teh bug")
    before = f.stat().st_mtime_ns
    fake_ctl.reply = {"action": "cancel"}
    assert claude_run(runtime, [str(f)]) == 0
    assert f.read_text() == "fix teh bug" and f.stat().st_mtime_ns == before


def test_editor_action_writes_then_execs(exec_spy, fake_ctl, tmp_path, runtime):
    f = tmp_path / PROMPT
    f.write_text("fix teh bug\n")
    fake_ctl.reply = {"action": "editor", "text": "Fix the bug."}
    assert claude_run(runtime, [str(f)]) == "exec"
    assert exec_spy.argv == ["vim", "-u", "NONE", str(f)]
    assert f.read_text() == "Fix the bug.\n"


def test_malformed_reply_falls_through(exec_spy, fake_ctl, tmp_path, runtime):
    f = tmp_path / PROMPT
    f.write_text("fix teh bug")
    for reply in ({"action": "weird"}, {"action": "apply"}, {"action": "apply", "text": 3},
                  {"ok": False, "error": "unknown request"}):
        fake_ctl.reply = reply
        exec_spy.argv = None
        assert claude_run(runtime, [str(f)]) == "exec", reply
        assert exec_spy.argv == ["vim", "-u", "NONE", str(f)]
        assert f.read_text() == "fix teh bug"


def test_undecodable_file_falls_through(exec_spy, tmp_path, runtime):
    f = tmp_path / PROMPT
    f.write_bytes(b"caf\xe9")
    assert claude_run(runtime, [str(f)]) == "exec"
    assert exec_spy.argv == ["vim", "-u", "NONE", str(f)] and f.read_bytes() == b"caf\xe9"


def test_missing_file_falls_through(exec_spy, tmp_path, runtime):
    assert claude_run(runtime, [str(tmp_path / PROMPT)]) == "exec"


def test_a_crash_exits_non_zero_and_leaves_the_file(fake_ctl, tmp_path, runtime, monkeypatch):
    f = tmp_path / PROMPT
    f.write_text("fix teh bug")
    fake_ctl.reply = {"action": "apply", "text": "Fix the bug."}

    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(shim.os, "replace", boom)
    assert claude_run(runtime, [str(f)]) not in (0, "exec")
    assert f.read_text() == "fix teh bug"
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(["rt", PROMPT])


def test_exec_failure_exits_non_zero(tmp_path, monkeypatch):
    f = tmp_path / "x.txt"
    f.write_text("x")
    env = {"GLANCE_REAL_EDITOR": "/nonexistent/editor-binary"}
    assert main_with(env, ["zsh"], [str(f)]) == 127 and f.read_text() == "x"


def test_shim_creates_no_runtime_dir(exec_spy, tmp_path):
    rt = tmp_path / "rt-empty"
    rt.mkdir()
    f = tmp_path / PROMPT
    f.write_text("x")
    claude_run(rt, [str(f)])
    assert list(rt.iterdir()) == []


# ---------------------------------------------------------------- never exec ourselves again


def test_self_is_recognised_behind_env_and_paths():
    for value in ("glance-edit", "/home/u/.local/bin/glance-edit --x",
                  "env glance-edit", "env -i FOO=1 /usr/bin/glance-edit", "FOO=1 glance-edit"):
        assert shim.real_editor({"EDITOR": value})[0] in shim.FALLBACK_EDITORS, value


def test_child_env_drops_editors_that_are_us_and_sets_the_guard(exec_spy, tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("x")
    main_with({"EDITOR": "glance-edit", "VISUAL": "env glance-edit", "PATH": "/usr/bin:/bin"},
              ["zsh"], [str(f)])
    assert "EDITOR" not in exec_spy.env and "VISUAL" not in exec_spy.env
    assert exec_spy.env["GLANCE_EDIT_DEPTH"] == "1"
    main_with({"EDITOR": "vim", "VISUAL": "glance-edit"}, ["zsh"], [str(f)])
    assert exec_spy.argv[0] == "vim" and exec_spy.env["EDITOR"] == "vim"
    assert "VISUAL" not in exec_spy.env


def test_guard_set_execs_a_plain_fallback(exec_spy, tmp_path):
    f = tmp_path / PROMPT
    f.write_text("x")
    env = {**CLAUDE_ENV, "GLANCE_EDIT_DEPTH": "1", "PATH": "/nonexistent"}
    assert main_with(env, ["claude"], [str(f)]) == "exec"
    assert exec_spy.argv == ["vi", str(f)]  # not GLANCE_REAL_EDITOR, not sensible-editor


def _stub(path, body):
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)


def _loop_rig(tmp_path, sensible_body):
    import sys

    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir / "glance-edit", f'exec "{sys.executable}" -m glance.edit_shim.main "$@"')
    _stub(bindir / "sensible-editor", sensible_body)
    _stub(bindir / "nano", 'printf NANO >> "$1"')
    _stub(bindir / "vi", 'printf VI >> "$1"')
    rt = tmp_path / "rt"
    rt.mkdir()
    f = tmp_path / "notes.txt"
    f.write_text("x")
    src = str(__import__("pathlib").Path(shim.__file__).parents[2])
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "EDITOR": "glance-edit",
           "VISUAL": "glance-edit", "XDG_RUNTIME_DIR": str(rt), "PYTHONPATH": src}
    return bindir, f, env


def test_no_exec_loop_when_every_editor_is_glance_edit(tmp_path):
    import subprocess

    # sensible-editor runs $VISUAL/$EDITOR, as Debian's does.
    bindir, f, env = _loop_rig(tmp_path, 'exec ${VISUAL:-${EDITOR:-nano}} "$@"')
    done = subprocess.run([str(bindir / "glance-edit"), str(f)], env=env, timeout=20,
                          capture_output=True, check=False)
    assert done.returncode == 0 and f.read_text() == "xNANO"


def test_no_exec_loop_when_the_fallback_insists_on_glance_edit(tmp_path):
    import subprocess

    # A sensible-editor that picks glance-edit by itself (~/.selected_editor): the guard.
    bindir, f, env = _loop_rig(tmp_path, f'exec "{tmp_path}/bin/glance-edit" "$@"')
    done = subprocess.run([str(bindir / "glance-edit"), str(f)], env=env, timeout=20,
                          capture_output=True, check=False)
    assert done.returncode == 0 and f.read_text() == "xNANO"


def test_edit_session_wait_is_capped(monkeypatch):
    assert shim.SESSION_TIMEOUT_S == 30 * 60
