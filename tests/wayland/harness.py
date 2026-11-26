"""A private headless GNOME Shell for the Wayland tests, and Glance running inside it.

Everything here must run inside `tests/wayland/private-session.sh` (it sets
GLANCE_WAYLAND_PRIVATE): a private HOME, XDG dirs, dconf and session bus. The shell is started
with `--headless --virtual-monitor`, so it never touches a real display, seat or session.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
UUID = "glance@btocode.github.io"
WAYLAND_DISPLAY = "glance-wl-test"
SIZE = (1280, 800)


def private_root() -> Path | None:
    """The scratch root of the private session, None outside one."""
    root = os.environ.get("GLANCE_WAYLAND_PRIVATE")
    if not root or not os.environ.get("HOME", "").startswith(root):
        return None
    return Path(root)


def wait_for(pred: Callable[[], Any], timeout_s: float, step_s: float = 0.05) -> Any:
    deadline = time.monotonic() + timeout_s
    while True:
        value = pred()
        if value:
            return value
        if time.monotonic() >= deadline:
            return value
        time.sleep(step_s)


def gsettings(*args: str) -> None:
    subprocess.run(["gsettings", *args], check=True, capture_output=True, timeout=20)


class Shell:
    """The headless GNOME Shell with Glance's extension enabled (test hooks on)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.proc: subprocess.Popen[bytes] | None = None
        self.log = root / "shell.log"
        self._bus: Any = None

    def install_extension(self) -> None:
        data = Path(os.environ["XDG_DATA_HOME"])
        dest = data / "gnome-shell" / "extensions" / UUID
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(REPO / "extension", dest)
        subprocess.run(["glib-compile-schemas", str(dest / "schemas")], check=True, timeout=30)
        gsettings("set", "org.gnome.shell", "disable-user-extensions", "false")
        gsettings("set", "org.gnome.shell", "enabled-extensions", f"['{UUID}']")
        gsettings("set", "org.gnome.shell", "disabled-extensions", "[]")
        gsettings("set", "org.gnome.desktop.interface", "toolkit-accessibility", "true")
        gsettings("set", "org.gnome.desktop.interface", "color-scheme", "default")
        # A new virtual pointer starts in the top-left corner: no overview from there.
        gsettings("set", "org.gnome.desktop.interface", "enable-hot-corners", "false")

    def start(self) -> Shell:
        self.install_extension()
        env = {**os.environ, "GLANCE_SHELL_TEST": "1"}
        env.pop("DISPLAY", None)
        self.proc = subprocess.Popen(
            ["gnome-shell", "--headless", "--wayland", "--no-x11", "--virtual-monitor",
             f"{SIZE[0]}x{SIZE[1]}", "--wayland-display", WAYLAND_DISPLAY],
            env=env, stdout=self.log.open("wb"), stderr=subprocess.STDOUT,
            start_new_session=True)
        from glance.shellbus import GioShellBus

        self._bus = GioShellBus()
        ok = wait_for(lambda: self._ping(), 60, 0.25)
        if not ok:
            raise RuntimeError("the headless shell or the extension did not come up; see "
                               f"{self.log}")
        self.call("TestHideOverview")
        return self

    def _ping(self) -> bool:
        from glance.shellbus import ShellUnavailable

        try:
            from gi.repository import GLib

            ctx = GLib.MainContext.default()
            while ctx.pending():
                ctx.iteration(False)
            self._bus._owner = self._bus._owner or self._owner()
            return bool(self._bus.call("Ping"))
        except ShellUnavailable:
            return False

    def _owner(self) -> str | None:
        try:
            from gi.repository import GLib

            reply = self._bus.connection.call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                "GetNameOwner", GLib.Variant("(s)", ("io.github.btocode.GlanceShell",)),
                None, 0, 500, None)
            return reply.unpack()[0]
        except Exception:  # noqa: BLE001
            return None

    def call(self, method: str, payload: Any = None, timeout_ms: int = 5000) -> Any:
        """Call the extension as the tests' user would: the test process is not Glance, so
        everything but Ping and the test hooks goes through the test-only TestCall."""
        self._bus._owner = self._bus._owner or self._owner()
        if method == "Ping" or method.startswith("Test"):
            return self._bus.call(method, payload, timeout_ms=timeout_ms)
        return self._bus.call("TestCall", {"method": method, "payload": payload or {}},
                              timeout_ms=timeout_ms)

    def call_directly(self, method: str, payload: Any = None) -> Any:
        """Call a method the way any other process on the bus would."""
        self._bus._owner = self._bus._owner or self._owner()
        return self._bus.call(method, payload)

    def overlay(self) -> dict[str, Any]:
        return self.call("TestOverlay")

    def focused(self) -> dict[str, Any] | None:
        return self.call("FocusedWindow").get("focus")

    def screenshot(self, path: Path) -> bool:
        return bool(self.call("TestScreenshot", {"path": str(path)}, 10000).get("ok"))

    def app_env(self) -> dict[str, str]:
        env = {**os.environ, "WAYLAND_DISPLAY": WAYLAND_DISPLAY, "GDK_BACKEND": "wayland"}
        env.pop("DISPLAY", None)
        return env

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)


class GtkApp:
    """tests/desktop/gtk_app.py on the headless shell; `buffer()` is its TextView's text."""

    def __init__(self, shell: Shell, text: str, *args: str) -> None:
        self.shell = shell
        self.proc = subprocess.Popen(
            [sys.executable, str(REPO / "tests" / "desktop" / "gtk_app.py"), "--text", text,
             *args], env=shell.app_env(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True)
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline().strip()
        if line != "READY":
            raise RuntimeError(f"the GTK test app did not start: {line!r}")
        wait_for(lambda: (self.shell.focused() or {}).get("pid") == self.proc.pid, 10)

    def buffer(self) -> str:
        assert self.proc.stdout is not None
        os.kill(self.proc.pid, signal.SIGUSR1)
        return json.loads(self.proc.stdout.readline())

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class Glance:
    """glanced (which starts glance-capture) with the fake AI backend; logs to files."""

    def __init__(self, shell: Shell, root: Path) -> None:
        self.shell = shell
        self.log = root / "glanced.log"
        env = {**shell.app_env(), "GLANCE_TEST": "1", "GLANCE_FAKE_BACKEND": "1",
               "GLANCE_STRICT_LOG": "1", "NO_AT_BRIDGE": "1"}
        env.pop("GLANCE_PAID", None)
        self.proc = subprocess.Popen([sys.executable, "-m", "glance.daemon.main"], env=env,
                                     stdout=self.log.open("wb"), stderr=subprocess.STDOUT,
                                     start_new_session=True)
        from glance.ipc import JsonLineClient
        from glance.paths import ctl_sock

        self._client = JsonLineClient(ctl_sock())
        if not wait_for(self._connect, 20, 0.2):
            raise RuntimeError(f"glanced did not start; see {self.log}")

    def _connect(self) -> bool:
        try:
            self._client.connect(timeout_s=0)
            return True
        except ConnectionError:
            return False

    def request(self, msg_type: str, payload: dict[str, Any] | None = None,
                timeout_s: float = 5.0) -> Any:
        return self._client.request(msg_type, payload or {}, timeout_s=timeout_s)

    def applies(self) -> list[str]:
        """The write-back results so far (`status=...` of each apply), from the log."""
        return [line.split()[1] for line in self.log.read_text(errors="replace").splitlines()
                if line.startswith("writeback_apply ")]

    def status(self) -> dict[str, Any]:
        return self.request("status")

    def stop(self) -> None:
        self._client.close()
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
