"""`glance start`: start glanced through the session's service manager.

Linux: the systemd user unit (`glance.service`). macOS: the launchd LaunchAgent, loaded
first when it is not (after a `launchctl bootout`). glanced itself is never spawned from
here, so it gets the same environment, logs and restart rules as at login.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

Runner = Callable[[Sequence[str]], int]
Probe = Callable[[], bool]
START_WAIT_S = 10.0  # Harper's data and the tray load in 1-3 s; a cold start can take longer
POLL_S = 0.2


def start_argvs(platform: str, uid: int, home: Path) -> list[list[str]]:
    """The commands to try in order; the first that succeeds starts glanced."""
    if platform == "darwin":
        from glance.platform.macos import launchd

        target = f"{launchd.domain(uid)}/{launchd.LABEL}"
        return [["launchctl", "kickstart", target],
                launchd.bootstrap_argv(uid, launchd.agent_path(home))]
    return [["systemctl", "--user", "start", "glance.service"]]


def hint(platform: str) -> str:
    if platform == "darwin":
        return "run `glance doctor --fix` to reinstall the login agent"
    return "see `journalctl --user -u glance -n 50`, or run `glance doctor`"


def _run(argv: Sequence[str]) -> int:
    try:
        return subprocess.run(list(argv), check=False, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode
    except OSError:
        return 127


def main(running: Probe, run: Runner = _run, platform: str = sys.platform,
         wait_s: float = START_WAIT_S, sleep: Callable[[float], None] = time.sleep) -> int:
    if running():
        print("Glance is already running")
        return 0
    if not any(run(argv) == 0 for argv in start_argvs(platform, os.getuid(), Path.home())):
        print(f"glance: could not start the Glance service; {hint(platform)}", file=sys.stderr)
        return 1
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if running():
            print("Glance started")
            return 0
        sleep(POLL_S)
    print(f"glance: the service started but glanced is not answering yet; {hint(platform)}",
          file=sys.stderr)
    return 1
