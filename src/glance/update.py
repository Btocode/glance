"""Updating Glance: run the published installer, and ask GitHub whether a newer one exists.

Two channels:

* release (the default): the newest GitHub Release tagged `v*` is compared with this
  version ("Glance 0.4.0 → 0.5.0"). Installs from a release tag, a tarball or a package.
* main (`glance update --main`): an install.sh clone that follows the main branch compares
  its commit with main's, as before releases existed. Also the fallback while no release has
  been published yet.

Only one request leaves the machine (the GitHub releases list, or `git ls-remote` for main);
no text is involved."""
from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from glance.log import log_event
from glance.version import is_newer, parse, version

INSTALL_URL = "https://raw.githubusercontent.com/Btocode/glance/main/install.sh"
REPO_URL = "https://github.com/Btocode/glance.git"
RELEASES_API = "https://api.github.com/repos/Btocode/glance/releases?per_page=30"
RELEASES_PAGE = "https://github.com/Btocode/glance/releases"
LOOKUP_TIMEOUT_S = 10.0
CHECK_FIRST_S = 60
CHECK_EVERY_S = 24 * 3600
EXIT_UPDATE_AVAILABLE = 10
EXIT_PACKAGED = 2

CURRENT, AVAILABLE, UNKNOWN = "current", "available", "unknown"
RELEASE, MAIN = "release", "main"

# run(argv, env, timeout) -> stdout, or None on any failure
Lookup = Callable[[list[str], Mapping[str, str], float], "str | None"]
# fetch(url, timeout) -> response body, or None on any failure
Fetch = Callable[[str, float], "str | None"]


@dataclass(frozen=True)
class UpdateCheck:
    status: str
    installed: str                 # this version, e.g. "0.4.0"
    latest: str | None = None      # newest release version, or main's commit (main channel)
    channel: str = RELEASE
    commit: str | None = None      # the installed commit (git installs)

    def line(self) -> str:
        """One line for `glance update --check` and doctor."""
        here = f"Glance {self.installed}"
        if self.channel == MAIN:
            mine = f"{here} (main {(self.commit or '')[:7]})"
            if self.status == AVAILABLE:
                return f"update available: {mine} → main {(self.latest or '')[:7]}"
            if self.status == CURRENT:
                return f"up to date: {mine}"
            return f"{mine}; could not check for updates (offline?)"
        if self.status == AVAILABLE:
            return f"update available: {here} → {self.latest}"
        if self.status == CURRENT:
            return f"up to date: {here}"
        return f"{here}; could not check for updates (offline?)"

    @property
    def label(self) -> str | None:
        """The tray's update item, when an update exists."""
        if self.status != AVAILABLE:
            return None
        if self.channel == MAIN:
            return "Update available (main) — Update now"
        return f"Update to Glance {self.latest} — Update now"


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


def _http_get(url: str, timeout: float) -> str | None:
    import urllib.request

    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "glance-update-check"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(2_000_000).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - offline, rate-limited, TLS: all "unknown"
        return None


def _fetch(url: str, timeout: float) -> str | None:
    """`_http_get`, resolved at call time (so tests can replace it module-wide)."""
    return _http_get(url, timeout)


def installed_commit(src: Path, run: Lookup = _lookup,
                     environ: Mapping[str, str] | None = None) -> str | None:
    if not (src / ".git").exists():
        return None
    out = run(["git", "-C", str(src), "rev-parse", "HEAD"], environ or os.environ,
              LOOKUP_TIMEOUT_S)
    return _sha(out)


def channel(src: Path, run: Lookup = _lookup, environ: Mapping[str, str] | None = None) -> str:
    """MAIN for a clone on a branch (install.sh --ref main, or any install before
    releases); RELEASE for a detached tag checkout, a tarball or a package."""
    if not (src / ".git").exists():
        return RELEASE
    out = run(["git", "-C", str(src), "symbolic-ref", "-q", "--short", "HEAD"],
              environ or os.environ, LOOKUP_TIMEOUT_S)
    return MAIN if (out or "").strip() else RELEASE


def latest_commit(run: Lookup = _lookup, environ: Mapping[str, str] | None = None) -> str | None:
    env = dict(environ or os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    out = run(["git", "ls-remote", REPO_URL, "refs/heads/main"], env, LOOKUP_TIMEOUT_S)
    return _sha(out)


def latest_release(fetch: Fetch = _fetch) -> str | None:
    """The newest published, non-prerelease `v*` release's version ("0.5.0"); "" when the
    list answers but holds none (core-v* releases do not count); None when it cannot be read."""
    body = fetch(RELEASES_API, LOOKUP_TIMEOUT_S)
    if body is None:
        return None
    try:
        items = json.loads(body)
    except ValueError:
        return None
    if not isinstance(items, list):
        return None
    found: list[str] = []
    for r in items:
        if not isinstance(r, dict) or r.get("draft") or r.get("prerelease"):
            continue
        tag = r.get("tag_name")
        if isinstance(tag, str) and len(tag) > 1 and tag[0] == "v" and tag[1].isdigit():
            found.append(tag[1:])
    return max(found, key=parse) if found else ""


def _sha(out: str | None) -> str | None:
    words = (out or "").split()
    if words and len(words[0]) >= 40 and all(c in "0123456789abcdef" for c in words[0]):
        return words[0]
    return None


def _main_check(mine: str, commit: str, run: Lookup, env: Mapping[str, str]) -> UpdateCheck:
    theirs = latest_commit(run, env)
    if theirs is None:
        return UpdateCheck(UNKNOWN, mine, None, MAIN, commit)
    return UpdateCheck(CURRENT if theirs == commit else AVAILABLE, mine, theirs, MAIN, commit)


def check(run: Lookup = _lookup, environ: Mapping[str, str] | None = None,
          fetch: Fetch = _fetch, current: str | None = None) -> UpdateCheck:
    """Never raises; any failure is `unknown`."""
    mine = current or version()
    try:
        env = os.environ if environ is None else environ
        if env.get("GLANCE_NO_UPDATE_CHECK") == "1":  # tests, packagers, air-gapped setups
            return UpdateCheck(UNKNOWN, mine)
        src = default_src_dir(env)
        commit = installed_commit(src, run, env)
        if commit is not None and channel(src, run, env) == MAIN:
            return _main_check(mine, commit, run, env)
        newest = latest_release(fetch)
        if newest is None:
            return UpdateCheck(UNKNOWN, mine, None, RELEASE, commit)
        if newest == "":  # nothing released yet: a clone follows main
            if commit is not None:
                return _main_check(mine, commit, run, env)
            return UpdateCheck(UNKNOWN, mine, None, RELEASE)
        return UpdateCheck(AVAILABLE if is_newer(newest, mine) else CURRENT, mine, newest,
                           RELEASE, commit)
    except Exception:  # noqa: BLE001 - a version lookup must never break its caller
        return UpdateCheck(UNKNOWN, mine)


def update_status(run: Lookup = _lookup, environ: Mapping[str, str] | None = None) -> str:
    return check(run, environ).status


# ------------------------------------------------------------------ installing

def packaged_by() -> str | None:
    """Set by a distribution package (AUR, Homebrew, .deb): a `_packaged` file next to this
    module, `<manager>|<update command>`, e.g. `AUR|yay -Syu glance-writing-assistant`."""
    try:
        text = (Path(__file__).resolve().parent / "_packaged").read_text(encoding="utf-8")
    except OSError:
        return None
    return text.strip() or None


def installer_command(ref: str = "latest") -> str:
    if ref not in ("latest", "main"):
        raise ValueError(f"not an update channel: {ref!r}")
    return f"curl -fsSL {INSTALL_URL} | bash -s -- --ref {ref}"


def installer_argv(ref: str = "latest") -> list[str]:
    return ["bash", "-c", installer_command(ref)]


def run_installer(popen: Callable[..., Any] | None = None, ref: str = "latest") -> int:
    """Run the published installer, its output streaming to ours; its exit code."""
    try:
        return int((popen or subprocess.Popen)(installer_argv(ref)).wait())
    except OSError as e:
        log_event("update_failed", kind=type(e).__name__)
        return 1


def report(result: UpdateCheck) -> tuple[str, int]:
    """(line for `glance update --check`, exit code)."""
    return result.line(), (EXIT_UPDATE_AVAILABLE if result.status == AVAILABLE else 0)


class UpdateState:
    """The last result of the daily check, kept in memory."""

    def __init__(self, lookup: Callable[[], Any] = check) -> None:
        self._lookup = lookup
        self.result = UpdateCheck(UNKNOWN, version())

    @property
    def status(self) -> str:
        return self.result.status

    def refresh(self) -> str:
        """Blocking: call from a background thread."""
        got = self._lookup()
        self.result = got if isinstance(got, UpdateCheck) else UpdateCheck(str(got), version())
        return self.status

    def update_args(self) -> list[str]:
        """`glance update` arguments that keep this install on the channel it follows."""
        return ["--main"] if self.result.channel == MAIN else []


def launch_update(notify: Callable[[str, str], None], log_file: Path,
                  popen: Callable[..., Any] = subprocess.Popen,
                  on_done: Callable[[bool], None] = lambda ok: None,
                  args: list[str] | None = None) -> bool:
    """`glance update` detached, its output in `log_file`; a notification when it ends.
    False when it could not even start."""
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        out = open(log_file, "ab")  # noqa: SIM115 - handed to the child, closed below
        try:
            proc = popen(["glance", "update", *(args or [])], stdin=subprocess.DEVNULL,
                         stdout=out, stderr=out, start_new_session=True)
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
