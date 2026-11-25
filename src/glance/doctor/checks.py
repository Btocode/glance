"""The doctor checks. Each is small, reads only through `DoctorEnv`, and says what to do next.

`run` never changes anything. `fix` / `undo` touch only what Glance owns (its own files, its
zsh block, its systemd unit) plus the one GNOME accessibility switch Glance needs.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from glance.backends.base import SIGN_IN
from glance.integrations import claude_wrapper
from glance.paths import harper_bin, overrides_manifest

HARPER_SHA256 = "c62ae24296005b835875f7e53238e05969feab68647f1c590d28c5de7fd8ff26"  # fetch-harper.sh
PY = "/usr/bin/python3"
FETCH_TIMEOUT_S = 180
APP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")
GSETTING = ["org.gnome.desktop.interface", "toolkit-accessibility"]
FIREFOX_SNAP_PLUGS = ("desktop", "desktop-legacy", "unity7", "gsettings")
# Browsers and chat apps Glance opts in by default; others are opt-in only.
DEFAULT_APPS = {"chrome": "Chrome", "google-chrome": "Chrome", "brave": "Brave",
                "brave-browser": "Brave", "slack": "Slack", "discord": "Discord"}
FLAG = "--force-renderer-accessibility"


@dataclass(frozen=True)
class CheckResult:
    id: str
    title: str
    status: str  # ok | warn | fail
    detail: str
    hint: str | None = None


def _no_healths() -> dict[str, bool]:
    return default_healths()


@dataclass
class DoctorEnv:
    home: Path
    environ: Mapping[str, str]
    run: Callable[..., tuple[int, str]]  # run(argv, timeout=None) -> (rc, output)
    procs: Callable[[], list[tuple[int, list[str]]]]
    ctl: Callable[[str, dict], dict | None]
    which: Callable[[str], str | None]
    healths: Callable[[], dict[str, bool]] = _no_healths  # backend name -> signed in
    install_override: Callable[[str], bool] | None = None  # tests inject; default: real override
    apps: tuple[str, ...] = field(default_factory=tuple)  # --app ids; set by apply_fixes

    @property
    def config_file(self) -> Path:
        base = self.environ.get("XDG_CONFIG_HOME") or str(self.home / ".config")
        return Path(base) / "glance" / "config.toml"

    @property
    def user_apps_dir(self) -> Path:
        base = self.environ.get("XDG_DATA_HOME") or str(self.home / ".local" / "share")
        return Path(base) / "applications"

    @property
    def system_app_dirs(self) -> list[Path]:
        raw = self.environ.get("XDG_DATA_DIRS", "")
        return [Path(d) / "applications" for d in raw.split(":") if d]

    @property
    def overrides_manifest(self) -> Path:
        return overrides_manifest(self.environ, self.home)

    def override_app(self, app_id: str) -> bool:
        """Opt one detected app in (by .desktop id or executable name)."""
        if self.install_override is not None:
            return self.install_override(app_id)
        from glance.doctor import desktop_entries as de

        want = app_id.casefold()
        hits = [t for t in de.find_targets(self.system_app_dirs, self.user_apps_dir)
                if t.id.casefold() == want or os.path.basename(t.binary).casefold() == want]
        for t in hits:
            de.install_override(t, self.user_apps_dir, self.overrides_manifest)
        return bool(hits)

    @property
    def zshrc(self) -> Path:
        return Path(self.environ.get("ZDOTDIR") or self.home) / ".zshrc"


def default_healths() -> dict[str, bool]:
    """Real per-backend health; any exception (an import included) means not connected."""
    from glance.backends.proc import ProcRunner

    runner = ProcRunner()

    def make(name: str) -> Any:
        if name == "claude":
            from glance.backends.claude import ClaudeBackend
            return ClaudeBackend(runner)
        if name == "codex":
            from glance.backends.codex import CodexBackend
            return CodexBackend(runner)
        if name == "gemini":
            from glance.backends.gemini import GeminiBackend
            return GeminiBackend(runner)
        if name == "opencode":
            from glance.backends.opencode import OpencodeBackend
            return OpencodeBackend(runner)
        from glance.backends.api import ApiBackend
        from glance.backends.keystore import SecretKeyStore
        return ApiBackend(SecretKeyStore())

    out: dict[str, bool] = {}
    for name in ("claude", "codex", "gemini", "opencode", "api"):
        try:
            h = make(name).health()
            out[name] = bool(h.installed and h.signed_in is True)
        except Exception:  # noqa: BLE001 - a broken backend is simply not connected
            out[name] = False
    return out


class Check(Protocol):
    id: str

    def run(self, env: DoctorEnv) -> CheckResult: ...
    def fix(self, env: DoctorEnv) -> bool: ...
    def undo(self, env: DoctorEnv) -> bool: ...


class _Base:
    id = ""
    title = ""

    def result(self, status: str, detail: str, hint: str | None = None) -> CheckResult:
        return CheckResult(self.id, self.title, status, detail, hint)

    def ok(self, detail: str = "ok") -> CheckResult:
        return self.result("ok", detail)

    def run(self, env: DoctorEnv) -> CheckResult:
        raise NotImplementedError

    def fix(self, env: DoctorEnv) -> bool:
        return False

    def undo(self, env: DoctorEnv) -> bool:
        return False


def _skip(env: DoctorEnv, var: str) -> bool:
    return bool(env.environ.get(var) or os.environ.get(var))


def _main_procs(env: DoctorEnv) -> list[tuple[int, list[str]]]:
    return [(pid, argv) for pid, argv in env.procs()
            if argv and not any(a.startswith("--type=") for a in argv)]


def _exe(argv: list[str]) -> str:
    return os.path.basename(argv[0]).casefold()


class Session(_Base):
    id, title = "session", "X11 session"

    def run(self, env):
        kind = env.environ.get("XDG_SESSION_TYPE", "")
        if kind != "x11" or not env.environ.get("DISPLAY"):
            return self.result("fail", f"session is {kind or 'unknown'}, DISPLAY "
                               f"{'set' if env.environ.get('DISPLAY') else 'missing'}",
                               "Glance needs an X11 session (log out, choose 'Ubuntu on Xorg')")
        return self.ok("X11")


class A11y(_Base):
    id, title = "a11y", "GNOME accessibility"

    def run(self, env):
        rc, out = env.run(["gsettings", "get", *GSETTING])
        if rc == 0 and out.strip() == "true":
            return self.ok("toolkit-accessibility is on")
        return self.result("fail", "toolkit-accessibility is off",
                           "run `glance doctor --fix` (gsettings set "
                           "org.gnome.desktop.interface toolkit-accessibility true)")

    def fix(self, env):
        if _skip(env, "GLANCE_SKIP_GSETTINGS"):
            return False
        return env.run(["gsettings", "set", *GSETTING, "true"])[0] == 0


class PythonDeps(_Base):
    id, title = "python", "System Python modules"
    SCRIPT = (
        "import gi, sys\n"
        "missing = []\n"
        "for ns, ver in (('Gtk','3.0'), ('Atspi','2.0'), ('Wnck','3.0'), ('Notify','0.7'),"
        " ('Secret','1')):\n"
        "    try:\n"
        "        gi.require_version(ns, ver)\n"
        "        __import__('gi.repository.' + ns)\n"
        "    except Exception:\n"
        "        missing.append(ns)\n"
        "try:\n"
        "    import Xlib\n"
        "except Exception:\n"
        "    missing.append('Xlib')\n"
        "print(' '.join(missing))\n"
        "sys.exit(1 if missing else 0)\n"
    )

    def run(self, env):
        rc, out = env.run([PY, "-c", self.SCRIPT])
        if rc == 0:
            return self.ok("Gtk 3, Atspi, Wnck, Notify, Secret, Xlib import")
        missing = out.strip() or "unknown module"
        return self.result("fail", f"cannot import: {missing}",
                           "install the matching python3-gi / gir1.2-* / python3-xlib packages")


class Harper(_Base):
    id, title = "harper", "Grammar engine (harper-ls)"

    def run(self, env):
        path = harper_bin()
        hint = "run `glance doctor --fix` (scripts/fetch-harper.sh)"
        if not path.is_file():
            return self.result("fail", f"{path} is missing", hint)
        if not os.access(path, os.X_OK):
            return self.result("fail", f"{path} is not executable", hint)
        if hashlib.sha256(path.read_bytes()).hexdigest() != HARPER_SHA256:
            return self.result("fail", f"{path} does not match the pinned SHA-256", hint)
        return self.ok("present, pinned checksum matches")

    def fix(self, env):
        src = Path(env.environ.get("GLANCE_SRC") or env.home / ".local" / "share" / "glance-src")
        script = src / "scripts" / "fetch-harper.sh"
        if not script.is_file():  # running from a checkout / the installed package dir
            script = Path(__file__).resolve().parents[3] / "scripts" / "fetch-harper.sh"
        return env.run(["bash", str(script)], timeout=FETCH_TIMEOUT_S)[0] == 0


class Tray(_Base):
    id, title = "tray", "Tray icon support"

    def run(self, env):
        rc, _ = env.run([PY, "-c", ("import gi; gi.require_version('AyatanaAppIndicator3','0.1');"
                          " from gi.repository import AyatanaAppIndicator3")])
        if rc == 0:
            return self.ok("AyatanaAppIndicator3 available")
        return self.result("warn", "no tray icon (AyatanaAppIndicator3 GIR missing)",
                           "sudo apt install gir1.2-ayatanaappindicator3-0.1")


class ConfigCheck(_Base):
    id, title = "config", "Configuration"

    def run(self, env):
        from glance.config import ConfigError, load_config

        try:
            load_config(env.config_file)
        except ConfigError as e:
            return self.result("fail", f"{e.key}: {e.reason}", f"fix {env.config_file}")
        return self.ok(str(env.config_file) if env.config_file.exists() else "defaults")


class Daemon(_Base):
    id, title = "daemon", "glanced"

    def run(self, env):
        st = env.ctl("status", {})
        if st is None:
            return self.result("fail", "glanced is not running", "systemctl --user start glance")
        if st.get("capture_connected") is False:
            return self.result("fail", "the capture helper is not connected",
                               "systemctl --user restart glance")
        if st.get("harper_alive") is False:
            return self.result("warn", "the grammar engine is not running",
                               "check `glance doctor` harper line, then restart glanced")
        return self.ok("running, capture connected")


class Hotkeys(_Base):
    id, title = "hotkeys", "Hotkeys"

    def run(self, env):
        from glance.config import ConfigError, load_config

        st = env.ctl("status", {})
        if st is None:
            return self.result("warn", "not checked: glanced is not running")
        try:
            wanted = load_config(env.config_file).general.hotkeys
        except ConfigError:
            return self.result("warn", "not checked: config is invalid")
        if "hotkeys_grabbed" not in st:
            return self.result("warn", "glanced does not report grabbed hotkeys",
                               "restart glanced after updating")
        grabbed = {n for n in str(st["hotkeys_grabbed"]).split(",") if n}
        lost = [f"{name} ({chord})" for name, chord in wanted.items() if name not in grabbed]
        if lost:
            return self.result("warn", "not grabbed: " + ", ".join(lost),
                               "another app owns it; change it in config.toml")
        return self.ok("all hotkeys grabbed")


class DesktopFlags(_Base):
    id, title = "desktop_flags", "App launcher flags"

    def _defaults(self, env):
        from glance.doctor import desktop_entries as de

        return [t for t in de.find_targets(env.system_app_dirs, env.user_apps_dir) if t.default]

    def run(self, env):
        from glance.doctor import desktop_entries as de

        user = env.user_apps_dir
        todo = [t for t in self._defaults(env) if de.override_state(t, user) in ("missing", "stale", "foreign")]
        launchers = de.unpatched_launchers(user)
        if todo or launchers:
            names = [t.name for t in todo] + [p.name for p in launchers]
            return self.result("fail", "launcher flag missing for: " + ", ".join(names),
                               "run `glance doctor --fix` (writes ~/.local/share/applications "
                               "overrides; undo with `glance doctor --undo`), then restart the app")
        if env.overrides_manifest.exists() or any(
                de.override_state(t, user) == "current" for t in self._defaults(env)):
            return self.ok("launchers flagged; restart the app to apply")
        return self.ok("no Chrome, Brave, Slack or Discord launchers found")

    def fix(self, env):
        from glance.doctor import desktop_entries as de

        user, manifest = env.user_apps_dir, env.overrides_manifest
        for t in self._defaults(env):
            if de.override_state(t, user) != "current":
                de.install_override(t, user, manifest)
        de.patch_user_launchers(user, manifest)
        return True

    def undo(self, env):
        from glance.doctor import desktop_entries as de

        de.undo_all(env.user_apps_dir, env.overrides_manifest)
        return True


class RunningFlags(_Base):
    id, title = "running_flags", "Running apps"

    def run(self, env):
        stale = sorted({DEFAULT_APPS[_exe(a)] for _, a in _main_procs(env)
                        if _exe(a) in DEFAULT_APPS and FLAG not in a})
        if stale:
            return self.result(
                "warn", "Restart " + ", ".join(stale) + " to enable Glance there",
                "optional: Glance wakes the accessibility tree itself; the launcher flag is "
                "a fallback that takes effect after a restart")
        return self.ok("no running app needs a restart")


class Firefox(_Base):
    id, title = "firefox", "Firefox"

    def run(self, env):
        snap = [a for _, a in _main_procs(env) if _exe(a) == "firefox" and a[0].startswith("/snap/")]
        if not snap:
            return self.ok("nothing needed; the tree is exposed on a plain launch")
        rc, out = env.run(["snap", "connections", "firefox"])
        missing = [p for p in FIREFOX_SNAP_PLUGS
                   if not re.search(rf"^{re.escape(p)}\s+firefox:{re.escape(p)}\s+(?!-(?:\s|$))\S", out, re.MULTILINE)]
        if rc == 0 and not missing:
            return self.ok("snap interfaces connected")
        return self.result("warn", "Firefox snap interface(s) not connected: "
                           + ", ".join(missing or FIREFOX_SNAP_PLUGS),
                           "check toolkit-accessibility first; untested fallbacks: start Firefox "
                           "with GNOME_ACCESSIBILITY=1, or set accessibility.force_disabled = 0")


class OtherElectron(_Base):
    id, title = "other_electron", "Other Chromium/Electron apps"
    opt_in = True  # `--fix --app ID` runs this even when the app is not running

    def detected(self, env) -> list[str]:
        renderers = {_exe(a) for _, a in env.procs() if a and "--type=renderer" in a}
        return sorted({_exe(a) for _, a in _main_procs(env)
                       if _exe(a) in renderers and _exe(a) not in DEFAULT_APPS})

    def run(self, env):
        found = self.detected(env)
        if not found:
            return self.ok("none running")
        return self.result("warn", "running without Glance support: " + ", ".join(found),
                           "opt in per app: glance doctor --fix --app " + found[0])

    def fix(self, env):
        done = [env.override_app(a) for a in env.apps
                if a not in DEFAULT_APPS and APP_ID.fullmatch(a)]
        return bool(done) and all(done)


class Backend(_Base):
    id, title = "backend", "AI backend"

    def run(self, env):
        from glance.config import ConfigError, load_config

        try:
            ready = {n for n, ok in env.healths().items() if ok}
        except Exception:  # noqa: BLE001 - any health failure means not connected
            ready = set()
        try:
            choice = load_config(env.config_file).backend.choice
        except ConfigError:
            choice = None
        if ready and choice in ready:
            return self.ok(f"{choice} connected")
        how = "; ".join(f"{n}: {c}" for n, c in SIGN_IN.items())
        if ready:
            return self.result("warn", "No AI backend chosen" if choice is None else
                               f"{choice} is not connected",
                               f"connected: {', '.join(sorted(ready))}; pick one with "
                               f"`glance backend use <name>`. Local grammar checks work.")
        return self.result("warn", "No AI connected; local grammar checks still work",
                           f"{how}; then `glance backend use <name>`")


class Wrapper(_Base):
    id, title = "wrapper", "Ctrl+G wrapper"

    def run(self, env):
        z = env.zshrc
        _, out = env.run(["zsh", "-ic", "print -r -- $EDITOR $VISUAL"])
        if "glance-edit" in out:  # the login shell's value, never doctor's own environment
            return self.result("fail", "$EDITOR/$VISUAL is globally set to glance-edit",
                               "remove that export; only the `claude` wrapper should set it")
        state = claude_wrapper.status(z)
        if state == "conflict":
            found = claude_wrapper.conflict_line(z)
            where = f"{z}:{found[0]}: {found[1]}" if found else str(z)
            return self.result(
                "warn", f"claude is already defined at {where}",
                "add this to your shell config by hand:\n"
                f"{claude_wrapper.BEGIN}\n{claude_wrapper.SNIPPET}\n{claude_wrapper.END}")
        if state == "absent":
            return self.result("warn", "claude wrapper not installed",
                               "run `glance doctor --fix` (or `glance wrapper install`)")
        return self.ok("installed")

    def fix(self, env):
        return claude_wrapper.install(env.zshrc) == "installed"

    def undo(self, env):
        return claude_wrapper.uninstall(env.zshrc)


class PathCheck(_Base):
    id, title = "path", "glance-edit on PATH"

    def run(self, env):
        rc, _ = env.run(["zsh", "-lc", "command -v glance-edit"])
        if rc == 0:
            return self.ok("found")
        return self.result("warn", "glance-edit is not on PATH in a login shell (Ctrl+G fails)",
                           'add to ~/.zprofile: export PATH="$HOME/.local/bin:$PATH", '
                           "then log out and in")


class Autostart(_Base):
    id, title = "autostart", "Start at login"

    def run(self, env):
        _, out = env.run(["systemctl", "--user", "is-enabled", "glance.service"])
        if out.strip() == "enabled":
            return self.ok("glance.service enabled")
        return self.result("warn", f"glance.service is {out.strip() or 'not installed'}",
                           "run `glance doctor --fix`")

    def fix(self, env):
        if _skip(env, "GLANCE_SKIP_SYSTEMCTL"):
            return False
        return env.run(["systemctl", "--user", "enable", "glance.service"])[0] == 0

    def undo(self, env):
        if _skip(env, "GLANCE_SKIP_SYSTEMCTL"):
            return False
        return env.run(["systemctl", "--user", "disable", "glance.service"])[0] == 0


class ClipboardHistory(_Base):
    id, title = "clipboard_history", "Clipboard history"

    def run(self, env):
        _, out = env.run(["gnome-extensions", "list", "--enabled"])
        if "clipboard" in out.casefold():
            return self.ok("a clipboard-history extension is on: long fixes are pasted with the "
                           "password-manager hint; short fixes are typed")
        return self.ok("no clipboard-history extension")


CHECKS: list[Check] = [
    Session(), A11y(), PythonDeps(), Harper(), Tray(), ConfigCheck(), Daemon(), Hotkeys(),
    DesktopFlags(), RunningFlags(), Firefox(), OtherElectron(), Backend(), Wrapper(),
    PathCheck(), Autostart(), ClipboardHistory(),
]
