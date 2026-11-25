import os
import shutil
import stat
import subprocess

import pytest

from glance.integrations.claude_wrapper import BEGIN, SNIPPET, install, status, uninstall


def test_install_is_idempotent(tmp_path):
    z = tmp_path / ".zshrc"
    z.write_text("export FOO=1\n")
    assert install(z) == "installed"
    assert install(z) == "installed"
    assert z.read_text().count(BEGIN) == 1
    assert status(z) == "installed"


def test_uninstall_restores_bytes_exactly(tmp_path):
    z = tmp_path / ".zshrc"
    for original in (
        "export FOO=1\n# comment\n\nalias ll='ls -l'\n",
        "export FOO=1",  # no trailing newline
        "a\n\n",
    ):
        z.write_text(original)
        install(z)
        assert uninstall(z)
        assert z.read_text() == original
    assert not uninstall(z)


def test_conflict_with_existing_function_or_alias(tmp_path):
    z = tmp_path / ".zshrc"
    for existing in (
        'claude() { command claude --foo "$@" }\n',
        "alias claude='claude --bar'\n",
        "function claude { :; }\n",
    ):
        z.write_text(existing)
        assert status(z) == "conflict"
        assert install(z) == "conflict"
        assert z.read_text() == existing


def test_status_absent(tmp_path):
    z = tmp_path / ".zshrc"
    assert status(z) == "absent"
    z.write_text("alias ll=ls\n# claude() mentioned in comment\n")
    assert status(z) == "absent"


def test_missing_zshrc_created_and_uninstall_leaves_empty_file(tmp_path):
    z = tmp_path / ".zshrc"
    assert install(z) == "installed"
    assert z.exists() and BEGIN in z.read_text()
    assert uninstall(z)
    assert z.read_text() == ""


def test_file_mode_preserved(tmp_path):
    z = tmp_path / ".zshrc"
    z.write_text("x=1\n")
    z.chmod(0o600)
    install(z)
    assert stat.S_IMODE(z.stat().st_mode) == 0o600
    uninstall(z)
    assert stat.S_IMODE(z.stat().st_mode) == 0o600


def test_snippet_does_not_export_editor():
    assert "export" not in SNIPPET and "command claude" in SNIPPET


@pytest.mark.skipif(shutil.which("zsh") is None, reason="needs zsh")
def test_wrapper_scopes_editor_to_claude_only(tmp_path):
    zdot = tmp_path / "zdot"
    zdot.mkdir()
    install(zdot / ".zshrc")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text('#!/bin/sh\necho "$EDITOR|$VISUAL|$GLANCE_REAL_EDITOR"\n')
    fake.chmod(0o755)
    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "ZDOTDIR": str(zdot),
        "HOME": str(tmp_path),
        "TERM": "dumb",
    }
    out = subprocess.run(
        ["zsh", "-ic", 'EDITOR=vim; unset VISUAL; claude; echo "after=$EDITOR"'],
        check=False,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    ).stdout.splitlines()
    assert "glance-edit|glance-edit|vim" in out
    assert "after=vim" in out


def test_cli_install_status_uninstall_and_conflict(tmp_path, monkeypatch, capsys):
    from glance.cli import main

    monkeypatch.setenv("ZDOTDIR", str(tmp_path))
    z = tmp_path / ".zshrc"
    assert main(["wrapper", "status"]) == 0 and capsys.readouterr().out.strip() == "absent"
    assert main(["wrapper", "install"]) == 0
    assert status(z) == "installed"
    assert main(["wrapper", "uninstall"]) == 0 and z.read_text() == ""
    z.write_text("x=1\nalias claude='claude --bar'\n")
    assert main(["wrapper", "install"]) == 2
    err = capsys.readouterr().err
    assert ":2:" in err and BEGIN in err


def test_non_utf8_bytes_round_trip(tmp_path):
    z = tmp_path / ".zshrc"
    original = b"# caf\xe9 latin-1\nexport A=1\n"
    z.write_bytes(original)
    assert install(z) == "installed"
    assert z.read_bytes().startswith(original)
    assert uninstall(z)
    assert z.read_bytes() == original


def test_crlf_round_trip_byte_exact(tmp_path):
    z = tmp_path / ".zshrc"
    original = b"export A=1\r\nalias ll=ls\r\n"
    z.write_bytes(original)
    install(z)
    assert z.read_bytes().startswith(original)
    assert uninstall(z)
    assert z.read_bytes() == original


def test_lone_begin_is_conflict_and_nothing_written(tmp_path):
    z = tmp_path / ".zshrc"
    content = f"a=1\n{BEGIN}\nclaude() {{ :; }}\n"
    z.write_text(content)
    assert status(z) == "conflict"
    assert install(z) == "conflict"
    with pytest.raises(ValueError):
        uninstall(z)
    assert z.read_text() == content


def test_symlinked_zshrc_stays_symlink(tmp_path):
    real = tmp_path / "dotfiles" / "zshrc"
    real.parent.mkdir()
    real.write_text("a=1\n")
    link = tmp_path / ".zshrc"
    link.symlink_to(real)
    install(link)
    assert link.is_symlink() and BEGIN in real.read_text()
    uninstall(link)
    assert link.is_symlink() and real.read_text() == "a=1\n"


def test_cli_missing_zdotdir_is_clean_error(tmp_path, monkeypatch, capsys):
    from glance.cli import main

    monkeypatch.setenv("ZDOTDIR", str(tmp_path / "nope"))
    assert main(["wrapper", "install"]) == 1
    assert "glance:" in capsys.readouterr().err


def test_new_file_respects_umask(tmp_path):
    old = os.umask(0o077)
    try:
        z = tmp_path / ".zshrc"
        install(z)
    finally:
        os.umask(old)
    assert stat.S_IMODE(z.stat().st_mode) == 0o600
