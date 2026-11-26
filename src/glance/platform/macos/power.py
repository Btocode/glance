"""On battery? On a Mac `pmset -g batt` says. Pure parsing plus one read-only call."""
from __future__ import annotations

import subprocess
from collections.abc import Callable


def parse_pmset(output: str) -> bool:
    """True when `pmset -g batt` says the power source is the battery."""
    first = output.splitlines()[0] if output else ""
    return "Battery Power" in first


def _read() -> str:
    try:
        done = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True,
                              timeout=2.0, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout if done.returncode == 0 else ""


def on_battery(read: Callable[[], str] = _read) -> bool:
    return parse_pmset(read())
