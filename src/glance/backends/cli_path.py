"""Where the AI CLIs live: the user's real PATH, not the service manager's.

glanced runs under systemd --user (launchd on macOS), whose PATH has none of the directories
a login shell adds (~/.local/bin, nvm, npm, bun...). So once per daemon start, and again when
the user asks for a refresh, the user's login + interactive shell prints its $PATH; the
directories CLIs are usually installed in are added to it. Every CLI backend looks its binary
up here and runs that absolute path with this PATH in the child's environment (node-based
CLIs need `node` on it too).

Only a constant script ever reaches the shell; the PATH itself is never logged, only how many
directories it has.
"""
from __future__ import annotations

import os
import pwd
import re
import shutil
import signal
import subprocess
import threading
from collections.abc import Callable, Mapping
from pathlib import Path

from glance.log import log_event

CLIS = ("claude", "codex", "gemini", "opencode")  # the backends that run a CLI, by binary
SHELL_TIMEOUT_S = 3.0
NOT_FOUND = "not found on your PATH or in the usual places"
MARK = "__GLANCE_PATH_7f3a__"
_SCRIPT = f"printf '%s%s%s\\n' '{MARK}' \"$PATH\" '{MARK}'"
_FISH_SCRIPT = f"printf '%s%s%s\\n' '{MARK}' (string join : $PATH) '{MARK}'"
_PARSE = re.compile(re.escape(MARK) + r"(.*?)" + re.escape(MARK))
# relative to $HOME, then absolute; only the ones that exist are used
HOME_DIRS = (".local/bin", ".claude/local", ".npm-global/bin", ".bun/bin", ".volta/bin",
             ".cargo/bin", ".opencode/bin", "go/bin", ".local/share/pnpm")
SYSTEM_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/snap/bin")

# (argv, timeout) -> stdout, or None when the shell failed / timed out
ShellRunner = Callable[[list[str], float], "str | None"]


def run_shell(argv: list[str], timeout_s: float) -> str | None:
    """Run the probe in its own session; on timeout the whole group is killed, so a stuck
    rc file (or a child it started that holds the pipe) never blocks the daemon."""
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, errors="replace",
                                cwd=str(Path.home()), start_new_session=True)
    except OSError:
        return None
    try:
        out, _ = proc.communicate(timeout=timeout_s)
        return out
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.communicate(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        return None
    finally:
        if proc.stdout:
            proc.stdout.close()


def login_shell(environ: Mapping[str, str]) -> str | None:
    """$SHELL, else the passwd entry's shell; None when neither is a usable program."""
    shell = environ.get("SHELL", "")
    if not shell:
        try:
            shell = pwd.getpwuid(os.getuid()).pw_shell
        except (KeyError, OSError):
            shell = ""
    if shell and os.path.isabs(shell) and os.access(shell, os.X_OK):
        return shell
    return None


def shell_argv(shell: str) -> list[str] | None:
    """The probe's argv: a login, interactive shell printing $PATH between two markers.
    csh-family shells cannot take -l with -c; they are skipped (the usual dirs still apply)."""
    name = os.path.basename(shell)
    if name == "fish":
        return [shell, "-l", "-i", "-c", _FISH_SCRIPT]
    if name in ("csh", "tcsh"):
        return None
    return [shell, "-l", "-i", "-c", _SCRIPT]


def parse(out: str | None) -> list[str]:
    """The absolute directories between the markers (the last pair: rc noise may come first)."""
    if not out:
        return []
    found = _PARSE.findall(out)
    if not found:
        return []
    return [d for d in found[-1].split(":") if d and os.path.isabs(d)]


def _version(name: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", name)
    return tuple(int(n) for n in nums) if nums else (-1,)


def well_known(home: Path) -> list[str]:
    """The directories CLIs are usually installed in that exist here, newest nvm node first."""
    out = [str(home / d) for d in HOME_DIRS]
    nvm = home / ".nvm" / "versions" / "node"
    try:
        versions = sorted((p for p in nvm.iterdir() if (p / "bin").is_dir()),
                          key=lambda p: _version(p.name), reverse=True)
    except OSError:
        versions = []
    if versions:
        out.append(str(versions[0] / "bin"))
    out += SYSTEM_DIRS
    return [d for d in out if os.path.isdir(d)]


def merge(*parts: list[str]) -> str:
    seen: set[str] = set()
    out = []
    for part in parts:
        for d in part:
            if d and d not in seen:
                seen.add(d)
                out.append(d)
    return os.pathsep.join(out)


def resolve(environ: Mapping[str, str] | None = None, home: Path | None = None,
            runner: ShellRunner | None = None) -> str:
    """The merged search PATH: the login shell's, then the current one, then the usual dirs."""
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else home
    shell = login_shell(environ)
    argv = shell_argv(shell) if shell else None
    run = runner or run_shell
    shell_dirs = parse(run(argv, SHELL_TIMEOUT_S)) if argv else []
    current = [d for d in environ.get("PATH", "").split(os.pathsep) if d]
    path = merge(shell_dirs, current, well_known(home))
    log_event("cli_path_resolved", shell=os.path.basename(shell) if shell else None,
              shell_dirs=len(shell_dirs), dirs=path.count(os.pathsep) + 1 if path else 0)
    return path


_lock = threading.Lock()
_cache: list[str] = []  # [path] once resolved


def search_path() -> str:
    """The cached merged PATH, resolved on first use."""
    with _lock:
        if not _cache:
            _cache.append(resolve())
        return _cache[0]


def refresh() -> None:
    """Forget the cached PATH: the next lookup asks the login shell again (`backend refresh`,
    the window's recheck: a CLI may have been installed since)."""
    with _lock:
        _cache.clear()


def find(binary: str) -> str | None:
    """The absolute path of an executable `binary`; a path is checked as given."""
    if os.sep in binary:
        return binary if os.path.isfile(binary) and os.access(binary, os.X_OK) else None
    return shutil.which(binary, path=search_path())


def child_env() -> dict[str, str]:
    """The environment additions for a spawned CLI."""
    return {"PATH": search_path()}


def where(binary: str) -> str:
    """A line for `glance doctor` / `glance backend list`."""
    found = find(binary)
    return f"found at {found}" if found else NOT_FOUND
