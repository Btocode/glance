"""The launchd LaunchAgent that runs glanced at login. Pure rendering plus thin launchctl argv.

No call here changes anything by itself: `render_plist` returns bytes, the `*_argv`
functions return argument lists. The installer and `glance doctor --fix` run them.
"""
from __future__ import annotations

import plistlib
from pathlib import Path

LABEL = "io.github.btocode.glance"


def agent_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def log_dir(home: Path) -> Path:
    return home / "Library" / "Logs" / "Glance"


def render_plist(glanced: Path, home: Path, path_env: str) -> bytes:
    """The agent: glanced at login, restarted after a crash (not after Quit), in the GUI
    session only, logs under ~/Library/Logs/Glance."""
    logs = log_dir(home)
    doc = {
        "Label": LABEL,
        "ProgramArguments": [str(glanced)],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "LimitLoadToSessionType": "Aqua",
        "ProcessType": "Interactive",
        "StandardOutPath": str(logs / "glanced.log"),
        "StandardErrorPath": str(logs / "glanced.log"),
        "EnvironmentVariables": {"PATH": path_env, "PYTHONUNBUFFERED": "1"},
    }
    return plistlib.dumps(doc, fmt=plistlib.FMT_XML, sort_keys=True)


def default_path_env(home: Path) -> str:
    """PATH for the agent: launchd's is bare, and the AI CLIs live in these places."""
    return ":".join([str(home / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin",
                     "/usr/bin", "/bin", "/usr/sbin", "/sbin"])


def domain(uid: int) -> str:
    return f"gui/{uid}"


def bootstrap_argv(uid: int, plist: Path) -> list[str]:
    return ["launchctl", "bootstrap", domain(uid), str(plist)]


def bootout_argv(uid: int) -> list[str]:
    return ["launchctl", "bootout", f"{domain(uid)}/{LABEL}"]


def kickstart_argv(uid: int) -> list[str]:
    return ["launchctl", "kickstart", "-k", f"{domain(uid)}/{LABEL}"]


def print_argv(uid: int) -> list[str]:
    return ["launchctl", "print", f"{domain(uid)}/{LABEL}"]


def parse_print(output: str) -> dict[str, str]:
    """`state` and `pid` (when running) from `launchctl print` output."""
    out: dict[str, str] = {}
    for line in output.splitlines():
        s = line.strip()
        for key in ("state", "pid", "last exit code"):
            if s.startswith(key + " = ") and key not in out:
                out[key] = s.split(" = ", 1)[1].strip()
    return out
