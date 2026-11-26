"""install.sh / uninstall.sh on macOS, run for real on Linux against a throwaway home.

`GLANCE_UNAME=Darwin` sends both scripts down their Darwin branch, which hands off to
scripts/install-macos.sh and scripts/uninstall-macos.sh. Nothing here touches the developer's
HOME, launchd or the network: every run gets `GLANCE_HOME` in tmp_path, a decoy `HOME` that
must stay empty, a logging `launchctl` shim first on PATH, a stub venv whose `python` and
`glance` run this repo's code, and the "curl | bash" case fetches install-macos.sh from a
file:// URL.
"""
from __future__ import annotations

import hashlib
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
NAMES = ("glance", "glanced", "glance-capture", "glance-edit")
AGENT = "Library/LaunchAgents/io.github.btocode.glance.plist"
MANIFEST = ".local/share/glance/install-manifest.macos"


def tree(root: Path) -> dict[str, str]:
    out = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if p.is_symlink():
            out[rel] = "-> " + os.readlink(p)
        elif p.is_file():
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
        elif p.is_dir():
            out[rel] = "dir"
    return out


def _script(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


class MacSandbox:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "home"
        self.decoy = tmp / "decoy"
        self.checkout = tmp / "checkout"
        self.shims = tmp / "shims"
        self.log = tmp / "shim.log"
        for d in (self.home, self.decoy, tmp / "cwd"):
            d.mkdir()
        self.log.write_text("")
        _script(self.shims / "launchctl", 'echo "launchctl $*" >> "$SHIM_LOG"\nexit 0\n')
        for name in ("systemctl", "sudo", "gsettings", "sw_vers", "xcode-select"):
            _script(self.shims / name, f'echo "{name} $*" >> "$SHIM_LOG"\nexit 1\n')
        self.checkout.mkdir()
        for f in ("install.sh", "uninstall.sh", "pyproject.toml"):
            shutil.copy2(REPO / f, self.checkout / f)
        for d in ("packaging", "scripts"):
            shutil.copytree(REPO / d, self.checkout / d)
        venv = self.checkout / ".venv"
        py = f'PYTHONPATH="{REPO / "src"}" exec "{sys.executable}"'
        _script(venv / "bin/python", f'{py} "$@"\n')
        _script(venv / "bin/glance", f'echo "glance $*" >> "$SHIM_LOG"\n{py} -m glance.cli "$@"\n')
        for name in NAMES[1:]:
            _script(venv / "bin" / name, "exit 0\n")
        self.env = {
            "PATH": f"{self.shims}:/usr/bin:/bin",
            "HOME": str(self.decoy),
            "GLANCE_HOME": str(self.home),
            "GLANCE_UNAME": "Darwin",
            "GLANCE_SKIP_SWVERS": "1",
            "GLANCE_SKIP_VENV": "1",
            "GLANCE_NO_UPDATE_CHECK": "1",
            "GLANCE_SKIP_FETCH": "1",
            "GLANCE_SKIP_DOCTOR": "1",
            "SHIM_LOG": str(self.log),
            "LANG": "C.UTF-8",
        }

    def bash(self, argv, env=None, stdin=None):
        return subprocess.run(["bash", *argv], check=False, env={**self.env, **(env or {})},
                              cwd=self.tmp / "cwd", text=True, capture_output=True, timeout=120,
                              start_new_session=True, input=stdin,
                              stdin=None if stdin is not None else subprocess.DEVNULL)

    def ok(self, script, *args, env=None):
        p = self.bash([str(self.checkout / script), *args], env=env)
        assert p.returncode == 0, f"{script} {args} -> {p.returncode}\n{p.stdout}\n{p.stderr}"
        return p

    def calls(self, prefix):
        return [ln for ln in self.log.read_text().splitlines() if ln.startswith(prefix)]


@pytest.fixture
def sb(tmp_path):
    s = MacSandbox(tmp_path)
    yield s
    assert list(s.decoy.iterdir()) == [], "something wrote to the real HOME"


def test_install_on_macos_writes_the_agent_links_and_manifest(sb):
    p = sb.ok("install.sh")
    assert "Accessibility" in p.stdout
    agent = sb.home / AGENT
    doc = plistlib.loads(agent.read_bytes())
    assert doc["ProgramArguments"] == [str(sb.checkout / ".venv/bin/glanced")]
    assert doc["StandardOutPath"] == str(sb.home / "Library/Logs/Glance/glanced.log")
    for name in NAMES:
        link = sb.home / ".local/bin" / name
        assert os.readlink(link) == str(sb.checkout / ".venv/bin" / name)
    manifest = (sb.home / MANIFEST).read_text()
    assert "platform\tmacos" in manifest and f"agent\t{agent}" in manifest
    uid = os.getuid()
    assert f"launchctl bootstrap gui/{uid} {agent}" in sb.calls("launchctl")
    assert not sb.calls("systemctl") and not sb.calls("sudo")
    assert (sb.home / "Library/Logs/Glance").is_dir()


def test_uninstall_reverses_the_install_byte_for_byte(sb):
    (sb.home / "Library/LaunchAgents").mkdir(parents=True)
    (sb.home / "Library/LaunchAgents/com.example.other.plist").write_bytes(b"<plist/>")
    (sb.home / ".local/bin").mkdir(parents=True)
    (sb.home / ".local/bin/mine").write_text("#!/bin/sh\n")
    (sb.home / ".config/glance").mkdir(parents=True)
    (sb.home / ".config/glance/config.toml").write_text("[ui]\n")
    before = tree(sb.home)
    sb.ok("install.sh")
    sb.ok("install.sh")  # again: an update must not grow what uninstall has to remove
    # what glanced and doctor leave at run time: sockets, the update log
    run = sb.home / ".local/state/glance/run"
    run.mkdir(parents=True, exist_ok=True)
    (run / "ctl.sock").write_text("")
    (sb.home / ".local/state/glance/update.log").write_text("checked\n")
    p = sb.ok("uninstall.sh")
    assert "Glance is uninstalled." in p.stdout
    assert tree(sb.home) == before
    assert f"launchctl bootout gui/{os.getuid()}/io.github.btocode.glance" in sb.calls(
        "launchctl")


def test_uninstall_from_an_empty_home_leaves_it_empty(sb):
    sb.ok("install.sh")
    (sb.home / ".local/state/glance/run").mkdir(parents=True, exist_ok=True)
    sb.ok("uninstall.sh")
    assert tree(sb.home) == {}


def test_uninstall_keep_data_keeps_the_dictionary(sb):
    sb.ok("install.sh")
    dic = sb.home / ".local/share/glance/dictionary.txt"
    dic.write_text("glanceword\n")
    sb.ok("uninstall.sh", "--keep-data")
    assert dic.read_text() == "glanceword\n"
    assert not (sb.home / MANIFEST).exists()
    assert not (sb.home / AGENT).exists()


def test_no_start_installs_the_agent_without_loading_it(sb):
    sb.ok("install.sh", "--no-start")
    assert (sb.home / AGENT).exists()
    assert not [c for c in sb.calls("launchctl") if "bootstrap" in c]


def test_a_foreign_command_is_never_overwritten(sb):
    (sb.home / ".local/bin").mkdir(parents=True)
    (sb.home / ".local/bin/glance").write_text("someone else's\n")
    p = sb.bash([str(sb.checkout / "install.sh")])
    assert p.returncode != 0 and "not Glance's" in p.stderr
    assert (sb.home / ".local/bin/glance").read_text() == "someone else's\n"
    assert not (sb.home / AGENT).exists()


def test_curl_pipe_bash_fetches_the_macos_installer(sb, tmp_path):
    # As `curl … install.sh | bash`: the script comes on stdin, so it has no checkout of its
    # own and must download install-macos.sh (here from a file:// URL) and a source.
    src = tmp_path / "src"
    shutil.copytree(sb.checkout, src, symlinks=True)
    script = (sb.checkout / "install.sh").read_text()
    p = sb.bash(["-s", "--"], stdin=script, env={
        "GLANCE_RAW_URL": f"file://{sb.checkout}", "GLANCE_SRC": str(src),
        "GLANCE_SKIP_CLONE": "1"})
    assert p.returncode == 0, p.stdout + p.stderr
    doc = plistlib.loads((sb.home / AGENT).read_bytes())
    assert doc["ProgramArguments"] == [str(src / ".venv/bin/glanced")]
    sb.ok("uninstall.sh")
    assert not (sb.home / AGENT).exists()


def test_linux_options_are_accepted_on_macos(sb):
    sb.ok("install.sh", "--yes", "--no-wrapper", "--no-desktop-overrides", "--no-start")


def test_the_macos_scripts_avoid_bash4_features():
    for name in ("install-macos.sh", "uninstall-macos.sh"):
        text = "\n".join(ln for ln in (REPO / "scripts" / name).read_text().splitlines()
                         if not ln.lstrip().startswith("#"))
        for bad in ("mapfile", "readarray", "declare -A", "local -A", "${!", ",,}", "^^}",
                    "|&", "&>>", "mv -T"):
            assert bad not in text, (name, bad)
    tail = (REPO / "install.sh").read_text().split("# >>> macOS", 1)[1]
    assert "mapfile" not in tail


def test_fetch_harper_pins_each_platform():
    text = (REPO / "scripts/fetch-harper.sh").read_text()
    for key in ("DARWIN_ARM64_BINARY_SHA256", "DARWIN_X86_64_BINARY_SHA256",
                "DARWIN_ARM64_TARBALL_SHA256", "DARWIN_X86_64_TARBALL_SHA256"):
        line = next(ln for ln in text.splitlines() if ln.startswith(key + '="'))
        assert len(line.split('"')[1]) == 64
    # the Linux pin keeps the exact line install.sh and doctor read
    assert any(ln.startswith('BINARY_SHA256="c62ae242') for ln in text.splitlines())


def test_setup_only_for_homebrew_writes_the_agent_for_the_formulas_glanced(sb):
    """`glance setup` after `brew install`: the agent runs the formula's glanced; nothing is
    linked or built; the uninstaller from the formula's share tree takes it back."""
    before = tree(sb.home)
    libexec = sb.tmp / "Cellar/glance/0.4.0/libexec"
    shutil.copytree(sb.checkout / ".venv", libexec)
    share = sb.tmp / "Cellar/glance/0.4.0/share/glance"
    shutil.copytree(sb.checkout, share, ignore=shutil.ignore_patterns(".venv"))
    p = sb.bash([str(share / "scripts/install-macos.sh"), "--setup-only", "--app-bin",
                 str(libexec / "bin")])
    assert p.returncode == 0, p.stdout + p.stderr
    doc = plistlib.loads((sb.home / AGENT).read_bytes())
    assert doc["ProgramArguments"] == [str(libexec / "bin/glanced")]
    assert not (sb.home / ".local/bin/glance").exists()
    assert "venv_created\t0" in (sb.home / MANIFEST).read_text()
    p = sb.bash([str(share / "scripts/uninstall-macos.sh")])
    assert p.returncode == 0, p.stdout + p.stderr
    assert tree(sb.home) == before
    assert (libexec / "bin/glanced").exists() and (share / "install.sh").exists()


def test_macos_ref_is_validated(sb):
    p = sb.bash([str(sb.checkout / "scripts/install-macos.sh"), "--ref", "bogus"],
                env={"GLANCE_SRC": str(sb.tmp / "src")})
    assert p.returncode != 0 and "--ref takes latest, main or a release tag" in p.stderr
