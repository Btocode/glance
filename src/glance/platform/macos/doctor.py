"""`glance doctor` on macOS: the checks that differ from Linux, plus the shared ones that apply.

Accessibility is granted per executable, and doctor usually runs from Terminal, whose own
permission says nothing about glanced's. So the permission is read from glanced's status
(`ax_trusted`, `glanced_exe`); when glanced is not running, doctor names the interpreter it
would run with (the venv's Python, resolved). `--fix` asks glanced to show macOS's dialog,
which then names the right binary, and opens the Accessibility pane; glanced itself asks
macOS once at start and notices the switch going on. No check here changes
anything outside Glance's own files except loading its own LaunchAgent.
"""
from __future__ import annotations

import hashlib
import os
import platform
import sys
from pathlib import Path

from glance.doctor.checks import (
    Backend,
    CheckResult,
    ConfigCheck,
    DoctorEnv,
    GrammarEngine,
    Updates,
    _Base,
    _skip,
)
from glance.paths import harper_bin
from glance.platform.macos import launchd
from glance.platform.macos.ax_trust import AX_PANE, open_pane_argv

# scripts/fetch-harper.sh pins these (tests check they agree).
HARPER_SHA256 = {
    "arm64": "cbe584977b11c7859743401eeef58c193e2debe97eec17103e661496e2499ba7",
    "x86_64": "701a2c49e15d98422fcafd973d7ecd0dd7d965b4bf0c4e5a81998bf3fca6c87e",
}
MIN_MACOS = 13


def interpreter() -> str:
    """The executable glanced runs as (the venv's python, symlinks resolved)."""
    return os.path.realpath(sys.executable)


def grant_hint(exe: str) -> str:
    return ("System Settings → Privacy & Security → Accessibility → + and add\n"
            f"{exe}\n(⌘⇧G in the file dialog, paste the path), switch it on, then run\n"
            f"launchctl kickstart -k gui/{os.getuid()}/{launchd.LABEL}")


class Platform(_Base):
    id, title = "platform", "macOS"

    def run(self, env):
        ver = env.environ.get("GLANCE_MACOS_VERSION") or platform.mac_ver()[0] or "0"
        arch = env.environ.get("GLANCE_MACOS_ARCH") or platform.machine()
        try:
            major = int(ver.split(".")[0])
        except ValueError:
            major = 0
        if major < MIN_MACOS:
            return self.result("fail", f"macOS {ver} ({arch})", "Glance needs macOS 13 or later")
        if arch not in HARPER_SHA256:
            return self.result("fail", f"macOS {ver} on {arch}", "unsupported architecture")
        return self.ok(f"macOS {ver} on {arch}")


def asked_line(exe: str, opened: bool) -> str:
    """The one thing to do: macOS already lists the binary, switched off."""
    where = "Privacy & Security → Accessibility" + (" (opened for you)" if opened else "")
    return f"Glance asked macOS for access: switch on {os.path.basename(exe)} under {where}"


def switch_hint(exe: str, opened: bool, waiting: bool) -> str:
    lines = [] if opened else ["open it: glance doctor --fix"]
    lines.append(f"the entry is {exe}")
    lines.append("not in the list? click +, press ⌘⇧G and paste that path")
    if not waiting:  # an older glanced does not notice the switch by itself
        lines.append(f"then run launchctl kickstart -k gui/{os.getuid()}/{launchd.LABEL}")
    return "\n".join(lines)


class Accessibility(_Base):
    id, title = "accessibility", "Accessibility permission"

    def __init__(self) -> None:
        self.opened = False  # `--fix` opened the pane (the re-run after it says so)

    def run(self, env):
        st = env.ctl("status", {})
        if st is None:
            return self.result("warn", "not checked: glanced is not running",
                               "start it (launchctl kickstart -k gui/"
                               f"{os.getuid()}/{launchd.LABEL}); it will need:\n"
                               + grant_hint(interpreter()))
        exe = str(st.get("glanced_exe") or interpreter())
        trusted = st.get("ax_trusted")
        capture = st.get("capture_ax_trusted")
        if trusted is True and capture is not False:
            return self.ok(f"granted to {exe}")
        if trusted is None:
            return self.result("warn", "glanced does not report it (an older version?)",
                               grant_hint(exe))
        if trusted is True:  # glanced has it; its capture helper is starting again
            return self.result("warn", "granted; the capture helper is restarting",
                               "run `glance doctor` again in a few seconds")
        return self.result("fail", asked_line(exe, self.opened),
                           switch_hint(exe, self.opened, st.get("ax_waiting") is True))

    def fix(self, env):
        """Open the Accessibility pane (and, if glanced never asked macOS for this binary,
        have it show macOS's dialog, which adds the binary to the list). The user still has
        to switch it on: nothing can."""
        if _skip(env, "GLANCE_SKIP_TCC_PROMPT"):
            return False
        st = env.ctl("status", {}) or {}
        if st.get("ax_prompted") is not True:
            env.ctl("ax_prompt", {"prompt": True})
        self.opened = env.run(open_pane_argv())[0] == 0
        return False


class PythonDeps(_Base):
    id, title = "python", "Python and PyObjC"
    SCRIPT = (
        "import sys\n"
        "missing = []\n"
        "for m in ('objc', 'AppKit', 'Foundation', 'ApplicationServices', 'Quartz', 'Security'):\n"
        "    try:\n"
        "        __import__(m)\n"
        "    except Exception:\n"
        "        missing.append(m)\n"
        "v = sys.version_info[:2]\n"
        "if not (3, 10) <= v < (3, 14):\n"
        "    missing.append('python %d.%d' % v)\n"
        "print(' '.join(missing))\n"
        "sys.exit(1 if missing else 0)\n"
    )

    def run(self, env):
        rc, out = env.run([sys.executable, "-c", self.SCRIPT])
        if rc == 0:
            return self.ok(f"Python {platform.python_version()}, PyObjC imports")
        return self.result("fail", f"missing: {out.strip() or 'unknown'}",
                           "run the installer again (it installs the `macos` extra)")


class Harper(_Base):
    id, title = "harper", "Grammar engine (harper-ls)"

    def run(self, env):
        path = harper_bin()
        want = HARPER_SHA256.get(env.environ.get("GLANCE_MACOS_ARCH") or platform.machine())
        hint = "run `glance doctor --fix` (scripts/fetch-harper.sh)"
        if not path.is_file():
            return self.result("fail", f"{path} is missing", hint)
        if not os.access(path, os.X_OK):
            return self.result("fail", f"{path} is not executable", hint)
        if want is None or hashlib.sha256(path.read_bytes()).hexdigest() != want:
            return self.result("fail", f"{path} does not match the pinned SHA-256", hint)
        return self.ok("present, pinned checksum matches")

    def fix(self, env):
        script = Path(__file__).resolve().parents[4] / "scripts" / "fetch-harper.sh"
        return env.run(["bash", str(script)], timeout=180)[0] == 0


class Daemon(_Base):
    id, title = "daemon", "glanced"

    def run(self, env):
        kick = f"launchctl kickstart -k gui/{os.getuid()}/{launchd.LABEL}"
        st = env.ctl("status", {})
        if st is None:
            return self.result("fail", "glanced is not running", kick)
        if st.get("capture_connected") is False:
            return self.result("fail", "the capture helper is not connected", kick)
        if st.get("harper_alive") is False:
            return self.result("warn", "the grammar engine is not running",
                               "check the harper line, then " + kick)
        return self.ok("running, capture connected")


class LaunchAgent(_Base):
    id, title = "launchd", "Start at login (LaunchAgent)"

    def run(self, env):
        plist = launchd.agent_path(env.home)
        if not plist.is_file():
            return self.result("warn", f"{plist} is not installed",
                               "run the installer (install.sh)")
        rc, out = env.run(launchd.print_argv(os.getuid()))
        if rc != 0:
            return self.result("warn", "installed but not loaded",
                               "run `glance doctor --fix` (launchctl bootstrap)")
        info = launchd.parse_print(out)
        state = info.get("state", "unknown")
        if state != "running":
            return self.result("warn", f"loaded, {state} (last exit {info.get('last exit code', '?')})",
                               f"logs: {launchd.log_dir(env.home) / 'glanced.log'}")
        return self.ok(f"running (pid {info.get('pid', '?')})")

    def fix(self, env):
        if _skip(env, "GLANCE_SKIP_LAUNCHCTL"):
            return False
        plist = launchd.agent_path(env.home)
        if not plist.is_file():
            return False
        return env.run(launchd.bootstrap_argv(os.getuid(), plist))[0] == 0


class MacHotkeys(_Base):
    id, title = "hotkeys", "Hotkeys"

    def run(self, env):
        from glance.config import ConfigError, load_config
        from glance.platform.macos.hotkeys_mac import mac_hotkeys, symbols

        st = env.ctl("status", {})
        if st is None:
            return self.result("warn", "not checked: glanced is not running")
        try:
            cfg = load_config(env.config_file)
        except ConfigError:
            return self.result("warn", "not checked: config is invalid")
        wanted = mac_hotkeys(cfg.general.hotkeys)
        if not cfg.shot.enabled:
            wanted.pop("shot", None)  # Glance Shot is off: its chord is not registered
        grabbed = {n for n in str(st.get("hotkeys_grabbed") or "").split(",") if n}
        lost = [f"{name} ({symbols(chord)})" for name, chord in wanted.items()
                if name not in grabbed]
        if lost:
            return self.result("warn", "not available: " + ", ".join(lost),
                               "another app or macOS owns it; change it under "
                               "[general.hotkeys] in config.toml")
        return self.ok("all hotkeys registered")


def mac_healths() -> dict[str, bool]:
    """Per-backend health as `default_healths`, with the api key read from the Keychain."""
    from glance.doctor.checks import default_healths

    out = {k: v for k, v in default_healths().items() if k != "api"}
    try:
        from glance.backends.api import ApiBackend
        from glance.platform.macos.keychain import KeychainKeyStore

        h = ApiBackend(KeychainKeyStore()).health()
        out["api"] = bool(h.installed and h.signed_in is True)
    except Exception:  # noqa: BLE001
        out["api"] = False
    return out


def mac_env() -> DoctorEnv:
    from dataclasses import replace

    from glance.doctor.runner import default_env

    return replace(default_env(), procs=list, healths=mac_healths)


CHECKS = [Platform(), Accessibility(), PythonDeps(), Harper(), GrammarEngine(),
          ConfigCheck(), Daemon(),
          MacHotkeys(), LaunchAgent(), Backend(), Updates()]

__all__ = ["AX_PANE", "CHECKS", "CheckResult", "mac_env"]
