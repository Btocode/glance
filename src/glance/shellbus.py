"""The session-bus link to Glance's GNOME Shell extension (GNOME on Wayland).

Two interfaces, both with one JSON string in and (for GlanceShell1) one JSON string out:

- `io.github.btocode.GlanceShell1`, exported by the extension at SHELL_PATH under the
  well-known name SHELL_NAME. glanced and glance-capture call it: render the overlay, grab
  keys, read the focused window, type into it, use the clipboard.
- `io.github.btocode.Glance1`, exported by glanced at DAEMON_PATH under DAEMON_NAME. The
  extension calls it: clicks, key presses, window changes, sizes.

Text crosses this bus only between the user's own processes and is never logged here: errors
are logged by kind, never with a payload.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol

from glance.log import log_event

SHELL_NAME = "io.github.btocode.GlanceShell"
SHELL_PATH = "/io/github/btocode/GlanceShell"
SHELL_IFACE = "io.github.btocode.GlanceShell1"
DAEMON_NAME = "io.github.btocode.Glance"
DAEMON_PATH = "/io/github/btocode/Glance"
DAEMON_IFACE = "io.github.btocode.Glance1"
# glance-capture's own name: the extension accepts calls only from the owners of DAEMON_NAME
# and CAPTURE_NAME, and only once it has checked who they are (extension/peers.js).
CAPTURE_NAME = "io.github.btocode.GlanceCapture"
EXTENSION_UUID = "glance@btocode.github.io"
EXTENSION_VERSION = 1  # the protocol both sides speak; Ping reports the extension's

CALL_TIMEOUT_MS = 1500
SHELL_METHODS = (
    "Ping", "Render", "Grab", "Ungrab", "FocusedWindow", "Monitors", "KeyState", "TypeText",
    "Chord", "ClipboardSave", "ClipboardSet", "ClipboardRestore", "ReadPrimary",
    "ActivateWindow", "Ask", "Pointer",
    # GLANCE_SHELL_TEST=1 only (the private test shell); absent otherwise
    "TestHideOverview", "TestMoveWindow", "TestOverlay", "TestClick", "TestScreenshot",
    "TestAskText", "TestPointer", "TestCall",
)
DAEMON_METHODS = ("ExtensionHello", "WindowChanged", "GeometryChanged", "Clicked", "Key",
                  "Sized", "AskDone")


def _xml(iface: str, methods: tuple[str, ...], out: bool) -> str:
    ret = '<arg type="s" direction="out"/>' if out else ""
    body = "".join(f'<method name="{m}"><arg type="s" direction="in"/>{ret}</method>'
                   for m in methods)
    signal = ('<signal name="WindowChanged"><arg type="s"/></signal>'
              if iface == SHELL_IFACE else "")
    return f'<node><interface name="{iface}">{body}{signal}</interface></node>'


SHELL_XML = _xml(SHELL_IFACE, SHELL_METHODS, out=True)
DAEMON_XML = _xml(DAEMON_IFACE, DAEMON_METHODS, out=False)


class ShellUnavailable(Exception):
    """The extension is not on the bus, or did not answer in time."""


class ShellBus(Protocol):
    """What the shell adapters need: one call, and whether the extension is there."""

    @property
    def available(self) -> bool: ...

    def call(self, method: str, payload: Any = None, timeout_ms: int = CALL_TIMEOUT_MS) -> Any:
        """Call `method` with `payload` as JSON; the decoded JSON answer.

        Raises ShellUnavailable when the extension is missing or does not answer."""
        ...


def encode(payload: Any) -> str:
    return json.dumps({} if payload is None else payload, ensure_ascii=False,
                      separators=(",", ":"))


def decode(answer: str) -> Any:
    try:
        return json.loads(answer) if answer else None
    except ValueError:
        return None


class GioShellBus:
    """ShellBus over Gio on the session bus. Watches SHELL_NAME: `available` follows it, and
    `on_change(available)` is called (on the GLib main loop) whenever it appears or goes."""

    def __init__(self, on_change: Callable[[bool], None] | None = None,
                 connection: Any = None) -> None:
        from gi.repository import Gio

        self._gio = Gio
        self._conn = connection or Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._owner: str | None = None
        self._on_change = on_change
        self._signal_ids: list[int] = []
        self._watch = Gio.bus_watch_name_on_connection(
            self._conn, SHELL_NAME, Gio.BusNameWatcherFlags.NONE,
            self._appeared, self._vanished)
        # bus_watch reports asynchronously; ask once now so the first calls already know
        try:
            reply = self._conn.call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                "GetNameOwner", _variant("(s)", (SHELL_NAME,)), None,
                Gio.DBusCallFlags.NONE, 500, None)
            self._owner = reply.unpack()[0]
        except Exception:  # noqa: BLE001 - not there (yet)
            self._owner = None

    @property
    def available(self) -> bool:
        return self._owner is not None

    @property
    def owner(self) -> str | None:
        """The extension's unique bus name, None while it is not there."""
        return self._owner

    @property
    def connection(self) -> Any:
        return self._conn

    def _appeared(self, _conn: Any, _name: str, owner: str) -> None:
        was = self.available
        self._owner = owner
        log_event("shell_extension_up")
        if self._on_change is not None and not was:
            self._on_change(True)

    def _vanished(self, _conn: Any, _name: str) -> None:
        was = self.available
        self._owner = None
        if was:
            log_event("shell_extension_down")
            if self._on_change is not None:
                self._on_change(False)

    def call(self, method: str, payload: Any = None, timeout_ms: int = CALL_TIMEOUT_MS) -> Any:
        if self._owner is None:
            raise ShellUnavailable(method)
        try:
            reply = self._conn.call_sync(
                SHELL_NAME, SHELL_PATH, SHELL_IFACE, method,
                _variant("(s)", (encode(payload),)), None,
                self._gio.DBusCallFlags.NONE, timeout_ms, None)
        except Exception as e:
            log_event("shell_call_failed", method=method, kind=type(e).__name__)
            raise ShellUnavailable(method) from e
        return decode(reply.unpack()[0])

    def subscribe(self, signal: str, fn: Callable[[Any], None]) -> None:
        """Call `fn(decoded payload)` for each `signal` the extension emits."""
        def on_signal(_conn: Any, _sender: Any, _path: Any, _iface: Any, _name: Any,
                      params: Any) -> None:
            try:
                fn(decode(params.unpack()[0]))
            except Exception as e:  # noqa: BLE001 - a handler must not raise into GLib
                log_event("shell_signal_error", kind=type(e).__name__)

        # Only from the extension: GDBus matches the well-known name to its current owner.
        self._signal_ids.append(self._conn.signal_subscribe(
            SHELL_NAME, SHELL_IFACE, signal, SHELL_PATH, None,
            self._gio.DBusSignalFlags.NONE, on_signal))

    def close(self) -> None:
        self._gio.bus_unwatch_name(self._watch)
        for sid in self._signal_ids:
            self._conn.signal_unsubscribe(sid)
        self._signal_ids.clear()


_REPLACE_EXISTING = 0x2  # org.freedesktop.DBus.RequestName flags
_PRIMARY_OWNER, _ALREADY_OWNER = 1, 4


def own_name(connection: Any, name: str) -> bool:
    """Take `name` on `connection` now (RequestName, synchronously), so that the extension
    sees this process as its owner before the first call; False when another process holds
    it and does not let go (the extension will then refuse this process)."""
    try:
        reply = connection.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "RequestName", _variant("(su)", (name, _REPLACE_EXISTING)), None, 0, 2000, None)
    except Exception as e:  # noqa: BLE001 - GLib.Error
        log_event("bus_name_failed", kind=type(e).__name__)
        return False
    ok = reply.unpack()[0] in (_PRIMARY_OWNER, _ALREADY_OWNER)
    if not ok:
        log_event("bus_name_taken", name=name)
    return ok


def peer_credentials(connection: Any, unique_name: str) -> tuple[int, int] | None:
    """(uid, pid) of the process behind a unique bus name, from the bus itself."""
    try:
        reply = connection.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "GetConnectionCredentials", _variant("(s)", (unique_name,)), None, 0, 1000, None)
    except Exception:  # noqa: BLE001 - gone, or not a name
        return None
    creds = reply.unpack()[0]
    uid, pid = creds.get("UnixUserID"), creds.get("ProcessID")
    if not isinstance(uid, int) or not isinstance(pid, int) or not pid:
        return None
    return uid, pid


def cmdline(pid: int) -> list[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return []
    return [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]


def is_gnome_shell(args: list[str]) -> bool:
    """The command line of GNOME Shell itself."""
    return bool(args) and args[0].rsplit("/", 1)[-1] == "gnome-shell"


def _variant(sig: str, value: Any) -> Any:
    from gi.repository import GLib

    return GLib.Variant(sig, value)


def gnome_shell_version(env: Any = None) -> int | None:
    """The major version of the GNOME Shell this session runs, from `gnome-shell --version`;
    None when it cannot be told."""
    import re
    import subprocess

    try:
        out = subprocess.run(["gnome-shell", "--version"], capture_output=True, text=True,
                             timeout=5, check=False, env=env).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"GNOME Shell (\d+)", out)
    return int(m.group(1)) if m else None
