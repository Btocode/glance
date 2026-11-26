"""glanced's end of the link to Glance's GNOME Shell extension (GNOME on Wayland).

`ShellLink` exports `io.github.btocode.Glance1` on the session bus under the well-known name
`io.github.btocode.Glance`. The extension calls it for clicks, key presses, window changes,
the sizes of what it drew and the "Rewrite as…" answer; each call is handed, decoded, to its
handler on the main loop. Calls from anyone but the extension are refused: the caller must
be the current owner of `io.github.btocode.GlanceShell`, and the bus must say that connection
is a gnome-shell process of this user. No payload is ever logged.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from glance.log import log_event
from glance.shellbus import (
    DAEMON_IFACE,
    DAEMON_METHODS,
    DAEMON_NAME,
    DAEMON_PATH,
    DAEMON_XML,
    SHELL_NAME,
    cmdline,
    decode,
    is_gnome_shell,
    own_name,
    peer_credentials,
)

Handler = Callable[[dict[str, Any]], None]


def dispatch(handlers: dict[str, Handler], method: str, raw: str) -> bool:
    """Call the handler of `method` with the decoded payload; False if it is not one."""
    handler = handlers.get(method)
    if handler is None or method not in DAEMON_METHODS:
        return False
    payload = decode(raw)
    try:
        handler(payload if isinstance(payload, dict) else {})
    except Exception as e:  # noqa: BLE001 - one bad call must not take the link down
        log_event("shell_handler_error", method=method, kind=type(e).__name__)
    return True


class ShellLink:
    def __init__(self, handlers: dict[str, Handler], connection: Any,
                 shell_owner: Callable[[], str | None],
                 is_shell: Callable[[str], bool] | None = None) -> None:
        self._handlers = handlers
        self._conn = connection
        self._shell_owner = shell_owner
        self._is_shell = is_shell or self._gnome_shell_process
        self._checked: dict[str, bool] = {}  # unique name -> it is gnome-shell
        self._reg_id = 0

    def _gnome_shell_process(self, sender: str) -> bool:
        creds = peer_credentials(self._conn, sender)
        return creds is not None and creds[0] == os.getuid() and is_gnome_shell(cmdline(creds[1]))

    def allowed(self, sender: str) -> bool:
        owner = self._shell_owner()
        if owner is None or sender != owner:
            return False
        if sender not in self._checked:
            if len(self._checked) > 16:
                self._checked.clear()
            self._checked[sender] = self._is_shell(sender)
            if not self._checked[sender]:
                log_event("shell_owner_refused")
        return self._checked[sender]

    def start(self) -> None:
        from gi.repository import Gio

        info = Gio.DBusNodeInfo.new_for_xml(DAEMON_XML).lookup_interface(DAEMON_IFACE)
        self._reg_id = self._conn.register_object(DAEMON_PATH, info, self._on_call, None, None)
        own_name(self._conn, DAEMON_NAME)  # now: the extension checks who owns it

    def stop(self) -> None:
        if self._reg_id:
            self._conn.unregister_object(self._reg_id)
            self._reg_id = 0

    def _on_call(self, _conn: Any, sender: str, _path: str, _iface: str, method: str,
                 params: Any, invocation: Any) -> None:
        if not self.allowed(sender):
            invocation.return_dbus_error("org.freedesktop.DBus.Error.AccessDenied",
                                         f"only {SHELL_NAME} may call this")
            return
        invocation.return_value(None)
        try:
            raw = params.unpack()[0]
        except (IndexError, TypeError):
            raw = ""
        if not dispatch(self._handlers, method, raw):
            log_event("shell_call_unknown", method=method if method.isidentifier() else "?")
