"""install.sh / uninstall.sh run for real, but only ever against a throwaway home.

Nothing here touches the developer's HOME, systemd, sudo or desktop settings: every run gets
`GLANCE_HOME` in tmp_path, a decoy `HOME` that must stay empty, a private `XDG_RUNTIME_DIR`,
no D-Bus/DISPLAY, and a PATH whose first entry holds logging shims for `systemctl`, `sudo`,
`gsettings` and `gnome-extensions`. The "checkout" is a copy of the scripts with a stub
`.venv` whose `glance` runs this repo's real CLI.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pty
import shutil
import subprocess
import sys
import termios
from pathlib import Path

import pytest

from glance.doctor.desktop_entries import _is_chromium_app

REPO = Path(__file__).resolve().parents[1]
NAMES = ("glance", "glanced", "glance-capture", "glance-edit")
BEGIN = "# >>> glance claude wrapper >>>"
UNIT = ".config/systemd/user/glance.service"
MANIFEST = ".local/share/glance/install-manifest.json"
ZSHRC = b"# my zshrc\nexport EDITOR=vim\nalias ll='ls -l'"  # no trailing newline, on purpose
SLACK = b"[Desktop Entry]\nName=Slack (mine)\nExec=/usr/bin/slack %U\n"


def tree(root: Path) -> dict[str, str]:
    """Sorted {relative path: sha256} of every file (symlinks as `-> target`) under root."""
    out = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if p.is_symlink():
            out[rel] = "-> " + os.readlink(p)
        elif p.is_file():
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _script(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def make_stub_venv(venv: Path) -> None:
    """A venv stand-in: `glance` and `python` run the real code, and log how they were called."""
    py = f'PYTHONPATH="{REPO / "src"}" exec "{sys.executable}"'
    _script(venv / "bin/python", 'echo "venv-python $*" >> "$SHIM_LOG"\n'
            '[ "$1 $2" = "-m pip" ] && exit 0\n'  # never a real pip: it would install for real
            f'{py} "$@"\n')
    _script(venv / "bin/glance", f'echo "glance $*" >> "$SHIM_LOG"\n{py} -m glance.cli "$@"\n')
    for name in NAMES[1:]:
        _script(venv / "bin" / name, "exit 0\n")


class Sandbox:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "home"
        self.decoy = tmp / "decoy-home"
        self.checkout = tmp / "checkout"
        self.shims = tmp / "shims"
        self.log = tmp / "shim.log"
        self.cwd = tmp / "cwd"
        for d in (self.home, self.decoy, self.cwd, tmp / "run"):
            d.mkdir()
        for name in ("systemctl", "sudo", "gsettings", "gnome-extensions", "claude"):
            _script(self.shims / name, f'echo "{name} $*" >> "$SHIM_LOG"\nexit 0\n')
        self.log.write_text("")
        self.checkout.mkdir()
        for f in ("install.sh", "uninstall.sh", "pyproject.toml"):
            if (REPO / f).exists():
                shutil.copy2(REPO / f, self.checkout / f)
        for d in ("packaging", "scripts"):
            if (REPO / d).exists():
                shutil.copytree(REPO / d, self.checkout / d)
        make_stub_venv(self.checkout / ".venv")
        self.env = {
            "PATH": f"{self.shims}:/usr/bin:/bin",
            "HOME": str(self.decoy),
            "GLANCE_HOME": str(self.home),
            "XDG_SESSION_TYPE": "x11",
            "XDG_RUNTIME_DIR": str(tmp / "run"),
            "SHIM_LOG": str(self.log),
            "GLANCE_SKIP_GSETTINGS": "1",
            "GLANCE_SKIP_FETCH": "1",
            "GLANCE_SKIP_VENV": "1",
            "skip_global_compinit": "1",  # Ubuntu's /etc/zsh/zshrc would write ~/.zcompdump
            "LANG": "C.UTF-8",
            "SHELL": "/usr/bin/zsh",  # with the claude shim: the wrapper question is asked
        }

    def bash(self, argv: list[str], *, env: dict[str, str] | None = None,
             stdin: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", *argv], check=False, env={**self.env, **(env or {})}, cwd=self.cwd, text=True,
            capture_output=True, timeout=180, start_new_session=True,  # no tty: no prompts
            input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL,
        )

    def run(self, script: str, *args: str, env: dict[str, str] | None = None,
            src: Path | None = None) -> subprocess.CompletedProcess:
        return self.bash([str((src or self.checkout) / script), *args], env=env)

    def ok(self, script: str, *args: str, **kw) -> subprocess.CompletedProcess:
        p = self.run(script, *args, **kw)
        assert p.returncode == 0, f"{script} {args} -> {p.returncode}\n{p.stdout}\n{p.stderr}"
        return p

    def calls(self, prefix: str) -> list[str]:
        return [ln for ln in self.log.read_text().splitlines() if ln.startswith(prefix)]

    def read(self, rel: str) -> str:
        return (self.home / rel).read_text()

    def seed(self) -> None:
        """What a real home already has: a zshrc, the user's own slack launcher, a tool."""
        (self.home / ".zshrc").write_bytes(ZSHRC)
        apps = self.home / ".local/share/applications"
        apps.mkdir(parents=True)
        (apps / "slack.desktop").write_bytes(SLACK)
        _script(self.home / ".local/bin/foo", "echo foo\n")

    def origin(self) -> Path:
        """The checkout as a git repository, to stand in for github.com/Btocode/glance."""
        repo = self.tmp / "origin"
        shutil.copytree(self.checkout, repo)
        self.git(repo, "init", "-q", "-b", "main")
        self.commit(repo, "initial")
        return repo

    def git(self, repo: Path, *args: str) -> str:
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.decoy), "GIT_CONFIG_GLOBAL": "/dev/null",
               "GIT_CONFIG_NOSYSTEM": "1"}
        return subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
             *args], env=env, check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, repo: Path, msg: str) -> None:
        self.git(repo, "add", "-A", "-f")
        self.git(repo, "commit", "-q", "-m", msg)


@pytest.fixture
def sb(tmp_path):
    box = Sandbox(tmp_path)
    yield box
    assert tree(box.decoy) == {}, "a script wrote to $HOME although GLANCE_HOME was set"
    assert box.calls("gsettings set") == []


def piped_env(sb: Sandbox, origin: Path) -> dict[str, str]:
    return {"GLANCE_REPO_URL": f"file://{origin}"}


def pipe_install(sb: Sandbox, origin: Path, *args: str) -> subprocess.CompletedProcess:
    """`curl … | bash -s -- ARGS`: the script arrives on stdin, there is no checkout."""
    text = (REPO / "install.sh").read_text()
    return sb.bash(["-s", "--", *args], stdin=text, env=piped_env(sb, origin))


# ------------------------------------------------------------------ install from a checkout


def test_install_creates_expected_files(sb):
    sb.ok("install.sh", "--no-start")
    t = tree(sb.home)
    for p in (".local/bin/glance", ".local/bin/glance-edit", UNIT, MANIFEST):
        assert p in t
    for name in NAMES:
        assert t[f".local/bin/{name}"] == f"-> {sb.checkout}/.venv/bin/{name}"
    unit = sb.read(UNIT)
    assert f"ExecStart={sb.checkout}/.venv/bin/glanced\n" in unit and "@VENV@" not in unit
    m = json.loads(sb.read(MANIFEST))
    assert m["src_dir"] == str(sb.checkout) and m["cloned"] is False
    assert m["venv_created"] is False and m["unit"] == str(sb.home / UNIT)
    assert {s["path"] for s in m["symlinks"]} == {str(sb.home / ".local/bin" / n) for n in NAMES}


def test_install_then_uninstall_restores_home_exactly(sb):
    sb.seed()
    before = tree(sb.home)
    sb.ok("install.sh", "--no-start", "--yes")
    assert BEGIN in sb.read(".zshrc")  # the run really changed the things uninstall must undo
    assert tree(sb.home) != before
    sb.ok("uninstall.sh")
    assert tree(sb.home) == before
    assert (sb.home / ".zshrc").read_bytes() == ZSHRC


def test_uninstall_removes_the_directories_install_created(sb):
    sb.ok("install.sh", "--no-start", "--yes")  # empty home: even .zshrc is new
    sb.ok("uninstall.sh")
    assert list(sb.home.iterdir()) == []


def test_install_is_rerunnable(sb):
    sb.seed()
    sb.ok("install.sh", "--no-start", "--yes")
    first = tree(sb.home)
    sb.ok("install.sh", "--no-start", "--yes")
    assert tree(sb.home) == first
    assert sb.read(".zshrc").count(BEGIN) == 1
    assert json.loads(sb.read(MANIFEST))["wrapper_installed"] is True
    sb.ok("uninstall.sh")
    assert (sb.home / ".zshrc").read_bytes() == ZSHRC


def test_install_refuses_to_overwrite_foreign_binary(sb):
    sb.seed()
    mine = sb.home / ".local/bin/glance"
    mine.write_text("#!/bin/sh\necho my own glance\n")
    before = tree(sb.home)
    p = sb.run("install.sh", "--no-start", "--yes")
    assert p.returncode != 0 and str(mine) in p.stderr
    assert tree(sb.home) == before  # refused before anything was written
    assert sb.calls("systemctl") == ["systemctl --user show-environment"]  # read-only probe


def test_install_refuses_a_symlink_to_something_else(sb):
    (sb.home / ".local/bin").mkdir(parents=True)
    (sb.home / ".local/bin/glanced").symlink_to("/opt/other/glanced")
    p = sb.run("install.sh", "--no-start")
    assert p.returncode != 0 and ".local/bin/glanced" in p.stderr
    assert os.readlink(sb.home / ".local/bin/glanced") == "/opt/other/glanced"


def test_install_reports_wrapper_conflict_without_writing(sb):
    zshrc = sb.home / ".zshrc"
    zshrc.write_text("alias claude='claude --verbose'\n")
    p = sb.ok("install.sh", "--no-start", "--yes")
    assert zshrc.read_text() == "alias claude='claude --verbose'\n"
    assert BEGIN in p.stdout and "by hand" in p.stdout  # the manual snippet
    assert json.loads(sb.read(MANIFEST))["wrapper_installed"] is False


def test_wrapper_is_not_installed_without_consent(sb):
    sb.seed()
    p = sb.ok("install.sh", "--no-start")  # no tty, no --yes: the answer is no
    assert (sb.home / ".zshrc").read_bytes() == ZSHRC
    assert "glance wrapper install" in p.stdout
    assert "--skip wrapper" in sb.calls("glance doctor --fix")[0]


def install_on_a_terminal(sb: Sandbox, typed: bytes, *args: str) -> tuple[int, str]:
    """`curl … | bash` at a terminal: the script on stdin, the person on /dev/tty."""
    master, slave = pty.openpty()

    def own_terminal() -> None:
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

    os.write(master, typed)
    try:
        p = subprocess.run(
            ["bash", "-s", "--", "--no-start", *args], check=False,
            env={**sb.env, **piped_env(sb, sb.origin())}, cwd=sb.cwd,
            input=(REPO / "install.sh").read_bytes(), capture_output=True, timeout=180,
            preexec_fn=own_terminal)
        asked = os.read(master, 4096).decode()
    finally:
        os.close(master)
        os.close(slave)
    return p.returncode, asked


def test_wrapper_question_is_asked_on_the_terminal_and_yes_installs(sb):
    sb.seed()
    rc, asked = install_on_a_terminal(sb, b"y\n")
    assert rc == 0 and "claude() wrapper" in asked and "[y/N]" in asked
    assert sb.read(".zshrc").count(BEGIN) == 1


def test_wrapper_question_defaults_to_no(sb):
    sb.seed()
    rc, asked = install_on_a_terminal(sb, b"\n")
    assert rc == 0 and "[y/N]" in asked
    assert (sb.home / ".zshrc").read_bytes() == ZSHRC


def test_no_wrapper_does_not_ask(sb):
    sb.seed()
    rc, asked = install_on_a_terminal(sb, b"y\n", "--no-wrapper")
    assert rc == 0 and "[y/N]" not in asked
    assert (sb.home / ".zshrc").read_bytes() == ZSHRC


def test_no_wrapper_wins_over_yes(sb):
    sb.seed()
    sb.ok("install.sh", "--no-start", "--yes", "--no-wrapper")
    assert (sb.home / ".zshrc").read_bytes() == ZSHRC


def test_opt_out_flags_reach_doctor(sb):
    sb.ok("install.sh", "--no-start", "--yes", "--no-desktop-overrides")
    (fix,) = sb.calls("glance doctor --fix")
    assert "--skip desktop_flags" in fix and "--skip wrapper" not in fix
    assert "--skip harper" in fix  # the installer fetched (or skipped) it itself


def test_install_prints_report_and_next_steps(sb):
    p = sb.ok("install.sh", "--no-start")
    assert "X11 session" in p.stdout  # the doctor report
    assert "glance doctor" in p.stdout
    assert "Connect an AI" in p.stdout
    for name in ("claude", "codex", "gemini", "opencode", "API key"):
        assert name in p.stdout


# ------------------------------------------------------------------ systemd


def test_install_enables_and_starts_the_unit(sb):
    sb.ok("install.sh")
    calls = sb.calls("systemctl")
    assert "systemctl --user daemon-reload" in calls
    assert "systemctl --user enable --now glance.service" in calls
    assert all(c.startswith("systemctl --user ") for c in calls)
    assert not [c for c in calls if "restart" in c]
    sb.ok("install.sh")  # an update must put the new code into service
    assert "systemctl --user restart glance.service" in sb.calls("systemctl")


def test_no_start_enables_without_starting(sb):
    sb.ok("install.sh", "--no-start")
    calls = sb.calls("systemctl")
    assert "systemctl --user enable glance.service" in calls
    assert not [c for c in calls if "--now" in c or "start" in c.split()[2:3]]


def test_skip_systemctl_hook(sb):
    env = {"GLANCE_SKIP_SYSTEMCTL": "1"}
    sb.ok("install.sh", env=env)
    sb.ok("uninstall.sh", env=env)
    # doctor still asks (read-only) whether the unit is enabled; nothing may change anything
    assert [c for c in sb.calls("systemctl") if "is-enabled" not in c] == []


def test_uninstall_stops_and_disables_the_unit(sb):
    sb.ok("install.sh")
    sb.log.write_text("")
    sb.ok("uninstall.sh")
    assert "systemctl --user disable --now glance.service" in sb.calls("systemctl")
    assert not (sb.home / UNIT).exists()


# ------------------------------------------------------------------ prerequisites


def test_refuses_outside_x11_and_writes_nothing(sb):
    p = sb.run("install.sh", env={"XDG_SESSION_TYPE": "wayland"})
    assert p.returncode != 0 and "X11" in p.stderr
    assert tree(sb.home) == {} and sb.log.read_text() == ""


def _fake_python(sb: Sandbox, missing: str) -> dict[str, str]:
    """A system python that has the right version but lacks some GI modules."""
    fake = sb.tmp / "fakepy"
    _script(fake, 'case "$2" in *version_info*) exit 0;; esac\n'
                  f'echo "{missing}"\nexit 1\n')
    return {"GLANCE_SYSTEM_PYTHON": str(fake)}


def test_missing_packages_print_the_apt_line_and_never_sudo(sb):
    p = sb.run("install.sh", env=_fake_python(sb, "gir1.2-atspi-2.0 gir1.2-wnck-3.0"))
    assert p.returncode != 0
    assert "sudo apt install gir1.2-atspi-2.0 gir1.2-wnck-3.0" in p.stderr
    assert "--with-apt" in p.stderr
    assert sb.calls("sudo") == [] and tree(sb.home) == {}


def test_with_apt_runs_only_apt_get_install(sb):
    p = sb.run("install.sh", "--with-apt", env=_fake_python(sb, "gir1.2-atspi-2.0"))
    assert sb.calls("sudo") == [
        "sudo apt-get install -y gir1.2-atspi-2.0 gir1.2-ayatanaappindicator3-0.1"]
    assert p.returncode != 0  # the fake python still lacks it: stop, do not loop
    assert tree(sb.home) == {}


def test_old_python_is_refused(sb):
    fake = sb.tmp / "oldpy"
    _script(fake, "exit 1\n")
    p = sb.run("install.sh", env={"GLANCE_SYSTEM_PYTHON": str(fake)})
    assert p.returncode != 0 and "3.10" in p.stderr and tree(sb.home) == {}


def test_python_314_is_refused_with_a_glance_message(sb):
    fake = sb.tmp / "py314"
    _script(fake, 'case "$2" in *version_info*) echo 3.14; exit 1;; esac\nexit 0\n')
    p = sb.run("install.sh", env={"GLANCE_SYSTEM_PYTHON": str(fake)})
    assert p.returncode != 0 and "3.13" in p.stderr and "3.14" in p.stderr
    assert tree(sb.home) == {}


def test_with_apt_without_apt_get_says_so_and_never_sudo(sb):
    nodpkg = sb.tmp / "usrbin-without-apt"
    nodpkg.mkdir()
    for entry in Path("/usr/bin").iterdir():
        if entry.name not in ("apt-get", "apt"):
            (nodpkg / entry.name).symlink_to(entry)
    env = {**_fake_python(sb, "gir1.2-atspi-2.0"), "PATH": f"{sb.shims}:{nodpkg}"}
    p = sb.run("install.sh", "--with-apt", env=env)
    assert p.returncode != 0 and "apt-get" in p.stderr and "gir1.2-atspi-2.0" in p.stderr
    assert sb.calls("sudo") == [] and tree(sb.home) == {}


def test_refuses_a_path_systemd_cannot_run(sb):
    odd = sb.tmp / "my checkout"
    shutil.copytree(sb.checkout, odd, symlinks=True)
    p = sb.run("install.sh", "--no-start", src=odd)
    assert p.returncode != 0 and "space" in p.stderr and str(odd) in p.stderr
    assert tree(sb.home) == {}


def test_refuses_without_a_systemd_user_manager(sb):
    _script(sb.shims / "systemctl", 'echo "systemctl $*" >> "$SHIM_LOG"\n'
            '[ "$2" = show-environment ] && exit 1\nexit 0\n')
    p = sb.run("install.sh", "--no-start")
    assert p.returncode != 0 and "systemd user manager" in p.stderr
    assert tree(sb.home) == {}
    assert sb.calls("systemctl") == ["systemctl --user show-environment"]


def test_refuses_other_architectures(sb):
    _script(sb.shims / "uname", "echo aarch64\n")
    p = sb.run("install.sh", "--no-start")
    assert p.returncode != 0 and "x86_64" in p.stderr and "aarch64" in p.stderr
    assert tree(sb.home) == {}


# ------------------------------------------------------------------ doctor exit codes


def test_red_checks_say_run_glance_doctor(sb):
    p = sb.ok("install.sh", "--no-start")  # the fake home has no AI and no unit: doctor exits 1
    assert "still red" in p.stdout and "run: glance doctor" in p.stdout


def test_doctor_crash_is_an_error_not_a_red_check(sb):
    glance = sb.checkout / ".venv/bin/glance"
    real = glance.read_text()
    _script(glance, 'case "$*" in *--fix*) echo "Traceback: boom" >&2; exit 1;; esac\n'
            + real.split("\n", 1)[1])
    p = sb.run("install.sh", "--no-start")
    assert p.returncode != 0 and "crashed" in p.stderr and "boom" in p.stderr
    assert "Glance is installed." not in p.stdout
    sb.ok("uninstall.sh")  # the record was written: everything comes out again
    assert tree(sb.home) == {}


# ------------------------------------------------------------------ claude wrapper offer


@pytest.mark.parametrize("why", ["no-claude", "bash"])
def test_wrapper_is_only_offered_to_zsh_users_with_claude(sb, why):
    sb.seed()
    env = {"SHELL": "/bin/bash"}
    if why == "no-claude":
        (sb.shims / "claude").unlink()
        env = {}
    p = sb.ok("install.sh", "--no-start", "--yes", env=env)
    assert (sb.home / ".zshrc").read_bytes() == ZSHRC
    assert "not offered" in p.stdout and "glance wrapper install" in p.stdout


# ------------------------------------------------------------------ venv and harper


def _venv_maker(sb: Sandbox) -> str:
    """Shell that builds the stub venv at "$dest" (shared by the fake uv and fake python)."""
    return (f'mkdir -p "$dest"\ncp -r "{sb.tmp}/venv-template/." "$dest/"\n'
            'echo "home = /usr/bin" > "$dest/pyvenv.cfg"\n')


def test_venv_is_built_with_uv_when_present(sb):
    shutil.move(sb.checkout / ".venv", sb.tmp / "venv-template")
    _script(sb.shims / "uv", 'echo "uv $*" >> "$SHIM_LOG"\n[ "$1" = venv ] || exit 0\n'
                             'for dest; do :; done\n' + _venv_maker(sb))
    venv = sb.checkout / ".venv"
    sb.ok("install.sh", "--no-start", env={"GLANCE_SKIP_VENV": ""})
    assert sb.calls("uv") == [
        f"uv venv --python /usr/bin/python3 --system-site-packages {venv}",
        f"uv pip install --python {venv}/bin/python -e {sb.checkout}",
    ]
    assert json.loads(sb.read(MANIFEST))["venv_created"] is True
    sb.log.write_text("")
    sb.ok("install.sh", "--no-start", env={"GLANCE_SKIP_VENV": ""})  # kept, only reinstalled
    assert [c.split()[1] for c in sb.calls("uv")] == ["pip"]
    assert json.loads(sb.read(MANIFEST))["venv_created"] is True
    sb.ok("uninstall.sh")
    assert not venv.exists() and (sb.checkout / "install.sh").exists()


def test_venv_falls_back_to_python_venv_without_uv(sb):
    shutil.move(sb.checkout / ".venv", sb.tmp / "venv-template")
    fake = sb.tmp / "syspy"
    _script(fake, 'if [ "$1" = -m ] && [ "$2" = venv ]; then\n  echo "syspy $*" >> "$SHIM_LOG"\n'
                  '  for dest; do :; done\n' + _venv_maker(sb) + '  exit 0\nfi\n'
                  'exec /usr/bin/python3 "$@"\n')
    venv = sb.checkout / ".venv"
    sb.ok("install.sh", "--no-start",
          env={"GLANCE_SKIP_VENV": "", "GLANCE_SYSTEM_PYTHON": str(fake)})
    assert sb.calls("syspy") == [f"syspy -m venv --system-site-packages {venv}"]
    assert f"venv-python -m pip install --quiet -e {sb.checkout}" in sb.calls("venv-python")


def test_harper_is_fetched_once_and_removed_on_uninstall(sb):
    sha = hashlib.sha256(b"harper\n").hexdigest()
    _script(sb.checkout / "scripts/fetch-harper.sh",
            f'BINARY_SHA256="{sha}"\necho "fetch" >> "$SHIM_LOG"\n'
            'd="${XDG_DATA_HOME:-$HOME/.local/share}/glance/bin"\nmkdir -p "$d"\n'
            'echo harper > "$d/harper-ls"\nchmod +x "$d/harper-ls"\n')
    sb.ok("install.sh", "--no-start", env={"GLANCE_SKIP_FETCH": ""})
    assert sb.read(".local/share/glance/bin/harper-ls") == "harper\n"
    sb.ok("install.sh", "--no-start", env={"GLANCE_SKIP_FETCH": ""})
    assert sb.calls("fetch") == ["fetch"]  # the pinned binary is already there
    sb.ok("uninstall.sh")
    assert tree(sb.home) == {}


# ------------------------------------------------------------------ uninstall


def test_uninstall_is_idempotent_and_safe_on_partial_install(sb):
    sb.seed()
    before = tree(sb.home)
    sb.ok("uninstall.sh")
    sb.ok("uninstall.sh")  # never installed: nothing to do, nothing touched
    assert tree(sb.home) == before
    sb.ok("install.sh", "--no-start", "--yes")
    (sb.home / MANIFEST).unlink()  # a half-finished install: no record, one link missing
    (sb.home / ".local/bin/glance-capture").unlink()
    sb.ok("uninstall.sh")
    assert tree(sb.home) == before
    sb.ok("uninstall.sh")
    assert tree(sb.home) == before


def test_uninstall_keep_data(sb):
    sb.ok("install.sh", "--no-start")
    words = sb.home / ".local/share/glance/dictionary.txt"
    words.write_text("Glance\n")
    cfg = sb.home / ".config/glance/config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("[general]\n")
    p = sb.ok("uninstall.sh", "--keep-data")
    assert words.read_text() == "Glance\n"
    assert cfg.read_text() == "[general]\n" and str(cfg) in p.stdout
    assert set(tree(sb.home)) == {
        ".local/share/glance/dictionary.txt", ".config/glance/config.toml"}


def test_uninstall_deletes_data_after_saying_so_and_keeps_config(sb):
    sb.ok("install.sh", "--no-start")
    (sb.home / ".local/share/glance/dictionary.txt").write_text("Glance\n")
    cfg = sb.home / ".config/glance/config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("[general]\n")
    p = sb.ok("uninstall.sh")
    assert "dictionary.txt" in p.stdout
    assert set(tree(sb.home)) == {".config/glance/config.toml"} and str(cfg) in p.stdout


def test_uninstall_leaves_foreign_symlink(sb):
    sb.ok("install.sh", "--no-start")
    link = sb.home / ".local/bin/glance"
    link.unlink()
    link.symlink_to("/opt/other/glance")
    sb.ok("uninstall.sh")
    assert os.readlink(link) == "/opt/other/glance"
    assert set(tree(sb.home)) == {".local/bin/glance"}


def _break_venv(sb: Sandbox) -> None:
    for name in ("glance", "python"):
        _script(sb.checkout / ".venv/bin" / name, "echo broken >&2\nexit 1\n")


@pytest.mark.skipif(
    not _is_chromium_app("/usr/bin/slack"),
    reason="needs a real Slack at /usr/bin/slack: the launcher override is only written for an "
           "installed Chromium/Electron app")
def test_uninstall_with_a_broken_venv_keeps_what_it_could_not_undo(sb):
    sb.seed()
    before = tree(sb.home)
    sb.ok("install.sh", "--no-start", "--yes")
    overrides = sb.home / ".local/share/glance/desktop-overrides.json"
    assert overrides.exists() and BEGIN in sb.read(".zshrc")
    _break_venv(sb)
    p = sb.run("uninstall.sh")
    assert p.returncode != 0 and "NOT fully uninstalled" in p.stdout
    assert str(overrides) in p.stdout and str(sb.home / ".zshrc") in p.stdout
    assert "Glance is uninstalled." not in p.stdout
    assert overrides.exists() and (sb.home / MANIFEST).exists()
    assert BEGIN in sb.read(".zshrc")
    make_stub_venv(sb.checkout / ".venv")  # repaired (a re-run of the installer does this)
    sb.ok("uninstall.sh")
    assert tree(sb.home) == before


def test_uninstall_falls_back_to_the_system_python(sb):
    sb.seed()
    before = tree(sb.home)
    sb.ok("install.sh", "--no-start", "--yes")
    shutil.copytree(REPO / "src", sb.checkout / "src",
                    ignore=shutil.ignore_patterns("__pycache__"))
    _break_venv(sb)
    p = sb.ok("uninstall.sh", env={"GLANCE_SYSTEM_PYTHON": sys.executable})
    assert "Glance is uninstalled." in p.stdout
    assert tree(sb.home) == before


def test_uninstall_removes_the_runtime_dir(sb):
    sb.ok("install.sh", "--no-start")
    assert (sb.tmp / "run/glance").is_dir()  # `glance doctor` asked for the control socket
    sb.ok("uninstall.sh")
    assert list((sb.tmp / "run").iterdir()) == []


# ------------------------------------------------------------------ curl … | bash


def test_install_from_stdin_clones_and_installs(sb):
    sb.seed()
    before = tree(sb.home)
    origin = sb.origin()
    p = pipe_install(sb, origin, "--no-start", "--yes")
    assert p.returncode == 0, p.stdout + p.stderr
    src = sb.home / ".local/share/glance-src"
    assert (src / ".git").is_dir() and sb.git(src, "rev-parse", "HEAD") == sb.git(
        origin, "rev-parse", "HEAD")
    m = json.loads(sb.read(MANIFEST))
    assert m["src_dir"] == str(src) and m["cloned"] is True
    assert os.readlink(sb.home / ".local/bin/glance") == f"{src}/.venv/bin/glance"
    assert f"ExecStart={src}/.venv/bin/glanced\n" in sb.read(UNIT)
    assert BEGIN in sb.read(".zshrc")
    assert list(sb.cwd.iterdir()) == []  # nothing dropped where the user happened to stand
    sb.ok("uninstall.sh", src=src)
    assert tree(sb.home) == before  # the clone goes too: the installer made it


def test_install_rerun_pulls_not_reclones(sb):
    origin = sb.origin()
    assert pipe_install(sb, origin, "--no-start").returncode == 0
    src = sb.home / ".local/share/glance-src"
    (src / "kept-by-pull").write_text("an untracked file a fresh clone would lose\n")
    (origin / "NEWS").write_text("v2\n")
    sb.commit(origin, "v2")
    p = pipe_install(sb, origin, "--no-start")
    assert p.returncode == 0, p.stdout + p.stderr
    assert (src / "NEWS").read_text() == "v2\n" and (src / "kept-by-pull").exists()
    assert json.loads(sb.read(MANIFEST))["cloned"] is True
    sb.ok("uninstall.sh", src=src)
    assert tree(sb.home) == {}


def test_install_from_tarball_when_git_is_missing(sb):
    origin = sb.origin()
    tar = sb.tmp / "main.tar.gz"
    with open(tar, "wb") as f:
        subprocess.run(["git", "-C", str(origin), "archive", "--format=tar.gz",
                        "--prefix=glance-main/", "HEAD"], check=True, stdout=f)
    env = {"GLANCE_NO_GIT": "1", "GLANCE_TARBALL_URL": f"file://{tar}"}
    text = (REPO / "install.sh").read_text()
    src = sb.home / ".local/share/glance-src"
    for run in range(2):  # the second run refreshes the same directory
        p = sb.bash(["-s", "--", "--no-start"], stdin=text, env=env)
        assert p.returncode == 0, p.stdout + p.stderr
        if run == 0:
            (src / ".venv/kept").write_text("the venv survives a refresh\n")
            (src / "stale").write_text("not in the tarball: gone after the refresh\n")
    assert (src / "pyproject.toml").exists() and not (src / ".git").exists()
    assert (src / ".venv/kept").exists() and not (src / "stale").exists()
    assert [x.name for x in src.parent.iterdir() if x.name.startswith("glance-src")] == [
        "glance-src"]  # no temporary directory left beside it
    assert json.loads(sb.read(MANIFEST))["cloned"] is True
    sb.ok("uninstall.sh", src=src)
    assert tree(sb.home) == {}


def test_failed_tarball_download_leaves_nothing_half_written(sb):
    env = {"GLANCE_NO_GIT": "1", "GLANCE_TARBALL_URL": f"file://{sb.tmp}/missing.tar.gz"}
    p = sb.bash(["-s", "--", "--no-start"], stdin=(REPO / "install.sh").read_text(), env=env)
    assert p.returncode != 0 and "could not download" in p.stderr
    share = sb.home / ".local/share"
    assert not (share / "glance-src").exists()
    assert not [x for x in share.iterdir() if x.name.startswith("glance-src")]


def test_install_refuses_a_source_dir_that_is_not_glance(sb):
    src = sb.home / ".local/share/glance-src"
    src.mkdir(parents=True)
    (src / "thesis.tex").write_text("precious\n")
    p = pipe_install(sb, sb.origin(), "--no-start")
    assert p.returncode != 0 and str(src) in p.stderr
    assert set(tree(sb.home)) == {".local/share/glance-src/thesis.tex"}


def test_from_local_needs_a_checkout(sb):
    text = (REPO / "install.sh").read_text()
    p = sb.bash(["-s", "--", "--from-local"], stdin=text)
    assert p.returncode != 0 and "checkout" in p.stderr and tree(sb.home) == {}


def test_moving_to_a_checkout_never_marks_it_for_deletion(sb):
    origin = sb.origin()
    assert pipe_install(sb, origin, "--no-start").returncode == 0
    clone = sb.home / ".local/share/glance-src"
    p = sb.ok("install.sh", "--no-start")  # the developer switches to their own checkout
    m = json.loads(sb.read(MANIFEST))
    assert m["src_dir"] == str(sb.checkout) and m["cloned"] is False
    assert os.readlink(sb.home / ".local/bin/glance") == f"{sb.checkout}/.venv/bin/glance"
    assert str(clone) in p.stdout  # told about the copy that is no longer used
    sb.ok("uninstall.sh")
    assert (sb.checkout / "install.sh").exists() and (sb.checkout / ".venv/bin/glance").exists()


def test_from_local_flag_installs_from_the_checkout(sb):
    sb.ok("install.sh", "--from-local", "--no-start")
    assert json.loads(sb.read(MANIFEST))["src_dir"] == str(sb.checkout)


def test_truncated_script_executes_nothing(sb):
    """A download cut anywhere must not run a partial installer."""
    text = (REPO / "install.sh").read_text()
    lines = text.splitlines(keepends=True)
    assert lines[-1].startswith("{ main") and lines[-1].endswith("}\n")
    cuts, pos = [], 0
    for line in lines[:-1]:
        pos += len(line)
        cuts.append(pos)
    cuts += range(pos + 1, len(text) - 1)  # every prefix of the last line, short of its `}`
    env = piped_env(sb, sb.origin())
    for cut in cuts:
        p = sb.bash(["-s", "--", "--no-start", "--yes"], stdin=text[:cut], env=env)
        assert p.stdout == "", f"cut at byte {cut} printed {p.stdout!r}"
        assert tree(sb.home) == {} and sb.log.read_text() == "", f"cut at byte {cut} ran"


# ------------------------------------------------------------------ unit, guard, lint


def test_unit_file_has_required_directives():
    u = (REPO / "packaging/glance.service").read_text()
    for s in ("PartOf=graphical-session.target", "After=graphical-session.target",
              "ExecStart=@VENV@/bin/glanced", "Restart=on-failure", "RestartSec=200ms",
              "RestartPreventExitStatus=78", "WantedBy=graphical-session.target"):
        assert s in u.splitlines()


@pytest.mark.parametrize("session", [{"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":7777"},
                                     {"XDG_SESSION_TYPE": "x11"}])
def test_unsupported_session_exits_78(tmp_path, session):
    # Never the real display: DISPLAY, where set, names a server that does not exist, and an
    # unguarded glanced cannot start (GDK_BACKEND=wayland, no socket in the private runtime dir).
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path),
           "GDK_BACKEND": "wayland", **session}
    p = subprocess.run([sys.executable, "-m", "glance.daemon.main"], env=env, check=False,
                       capture_output=True, timeout=60)
    assert p.returncode == 78 and b"X11" in p.stderr


@pytest.mark.parametrize("script", ["install.sh", "uninstall.sh"])
def test_scripts_parse_and_lint(script):
    path = REPO / script
    assert os.access(path, os.X_OK)
    assert subprocess.run(["bash", "-n", str(path)], check=False).returncode == 0
    if shutil.which("shellcheck"):
        p = subprocess.run(["shellcheck", str(path)], check=False, capture_output=True, text=True)
        assert p.returncode == 0, p.stdout
