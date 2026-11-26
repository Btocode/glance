"""Updating Glance: run the published installer, and ask GitHub whether a newer commit exists.

Nothing but one `git ls-remote` request leaves the machine; no text is involved."""
from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from glance.log import log_event

INSTALL_URL = "https://raw.githubusercontent.com/Btocode/glance/main/install.sh"
REPO_URL = "https://github.com/Btocode/glance.git"
LOOKUP_TIMEOUT_S = 10.0
CHECK_FIRST_S = 60
CHECK_EVERY_S = 24 * 3600
EXIT_UPDATE_AVAILABLE = 10

CURRENT, AVAILABLE, UNKNOWN = "current", "available", "unknown"

# run(argv, env, timeout) -> stdout, or None on any failure
Lookup = Callable[[list[str], Mapping[str, str], float], "str | None"]


def default_src_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Where install.sh keeps its clone: $GLANCE_SRC, else <data root>/glance-src."""
    env = os.environ if environ is None else environ
    if env.get("GLANCE_SRC"):
        return Path(env["GLANCE_SRC"])
    home = Path(env.get("GLANCE_HOME") or env.get("HOME") or Path.home())
    return Path(env.get("XDG_DATA_HOME") or home / ".local" / "share") / "glance-src"


def _git(argv: list[str], env: Mapping[str, str], timeout: float) -> str | None:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False,
                           stdin=subprocess.DEVNULL, env=dict(env))
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def _lookup(argv: list[str], env: Mapping[str, str], timeout: float) -> str | None:
    """`_git`, resolved at call time (so tests can replace it module-wide)."""
    return _git(argv, env, timeout)


def installed_commit(src: Path, run: Lookup = _lookup,
                     environ: Mapping[str, str] | None = None) -> str | None:
    if not (src / ".git").exists():
        return None
    out = run(["git", "-C", str(src), "rev-parse", "HEAD"], environ or os.environ,
              LOOKUP_TIMEOUT_S)
    return _sha(out)


def latest_commit(run: Lookup = _lookup, environ: Mapping[str, str] | None = None) -> str | None:
    env = dict(environ or os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    out = run(["git", "ls-remote", REPO_URL, "refs/heads/main"], env, LOOKUP_TIMEOUT_S)
    return _sha(out)


def _sha(out: str | None) -> str | None:
    words = (out or "").split()
    if words and len(words[0]) >= 40 and all(c in "0123456789abcdef" for c in words[0]):
        return words[0]
    return None


def check(run: Lookup = _lookup, environ: Mapping[str, str] | None = None
          ) -> tuple[str, str | None, str | None]:
    """(status, installed, latest). Never raises; any failure is `unknown`."""
    try:
        env = os.environ if environ is None else environ
        mine = installed_commit(default_src_dir(env), run, env)
        if mine is None:
            return UNKNOWN, None, None
        theirs = latest_commit(run, env)
        if theirs is None:
            return UNKNOWN, mine, None
        return (CURRENT if mine == theirs else AVAILABLE), mine, theirs
    except Exception:  # noqa: BLE001 - a version lookup must never break its caller
        return UNKNOWN, None, None


def update_status(run: Lookup = _lookup, environ: Mapping[str, str] | None = None) -> str:
    return check(run, environ)[0]


def installer_argv() -> list[str]:
    return ["bash", "-c", f"curl -fsSL {INSTALL_URL} | bash"]


def run_installer(popen: Callable[..., Any] | None = None) -> int:
    """Run the published installer, its output streaming to ours; its exit code."""
    try:
        return int((popen or subprocess.Popen)(installer_argv()).wait())
    except OSError as e:
        log_event("update_failed", kind=type(e).__name__)
        return 1


def report(status: str, installed: str | None, latest: str | None) -> tuple[str, int]:
    """(line for `glance update --check`, exit code)."""
    if status == AVAILABLE:
        return (f"update available (installed {(installed or '')[:7]}, "
                f"latest {(latest or '')[:7]})", EXIT_UPDATE_AVAILABLE)
    if status == CURRENT:
        return "up to date", 0
    return "unknown (not a git install)", 0


class UpdateState:
    """The last result of the daily check, kept in memory."""

    def __init__(self, lookup: Callable[[], str] = update_status) -> None:
        self._lookup = lookup
        self.status = UNKNOWN

    def refresh(self) -> str:
        """Blocking: call from a background thread."""
        self.status = self._lookup()
        return self.status


def launch_update(notify: Callable[[str, str], None], log_file: Path,
                  popen: Callable[..., Any] = subprocess.Popen,
                  on_done: Callable[[bool], None] = lambda ok: None) -> bool:
    """`glance update` detached, its output in `log_file`; a notification when it ends.
    False when it could not even start."""
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        out = open(log_file, "ab")  # noqa: SIM115 - handed to the child, closed below
        try:
            proc = popen(["glance", "update"], stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                         start_new_session=True)
        finally:
            out.close()
    except OSError as e:
        log_event("update_launch_failed", kind=type(e).__name__)
        notify("Glance update failed", "Run `glance update` in a terminal.")
        return False

    def wait() -> None:
        ok = proc.wait() == 0
        if ok:
            notify("Glance updated", "The new version is starting.")
        else:
            notify("Glance update failed", "Run `glance update` in a terminal.")
        on_done(ok)

    threading.Thread(target=wait, name="glance-update", daemon=True).start()
    return True
