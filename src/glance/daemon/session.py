"""Session events from the desktop: suspend/resume, screen lock and monitor hotplug.

    SessionEvents(on_sleep, on_resume, on_lock, on_monitors_changed).start()

All callbacks run on the GLib main loop. Nothing here raises when a bus or a signal is
missing (a desktop without logind or GNOME's screensaver): the events just never come.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from glance.log import log_event

LOGIND_PATH = "/org/freedesktop/login1"
LOGIND_IFACE = "org.freedesktop.login1.Manager"
SCREENSAVER_IFACE = "org.gnome.ScreenSaver"


class SessionEvents:
    def __init__(self, on_sleep: Callable[[], None], on_resume: Callable[[], None],
                 on_lock: Callable[[bool], None],
                 on_monitors_changed: Callable[[], None]) -> None:
        self._on_sleep = on_sleep
        self._on_resume = on_resume
        self._on_lock = on_lock
        self._on_monitors_changed = on_monitors_changed
        self._buses: list[Any] = []  # keep the connections (and so the subscriptions) alive

    # -- handlers (also what the unit tests call) ---------------------------------------

    def prepare_for_sleep(self, starting: bool) -> None:
        (self._on_sleep if starting else self._on_resume)()

    def active_changed(self, active: bool) -> None:
        self._on_lock(bool(active))

    def monitors_changed(self) -> None:
        self._on_monitors_changed()

    # -- wiring ----------------------------------------------------------------------

    def start(self) -> None:
        from gi.repository import Gdk, Gio

        self._subscribe(Gio.BusType.SYSTEM, LOGIND_IFACE, "PrepareForSleep",
                        lambda args: self.prepare_for_sleep(bool(args[0])))
        self._subscribe(Gio.BusType.SESSION, SCREENSAVER_IFACE, "ActiveChanged",
                        lambda args: self.active_changed(bool(args[0])))
        screen = Gdk.Screen.get_default()
        if screen is not None:
            screen.connect("monitors-changed", lambda *_a: self.monitors_changed())
            screen.connect("size-changed", lambda *_a: self.monitors_changed())

    def _subscribe(self, bus_type: Any, iface: str, signal: str,
                   handler: Callable[[tuple[Any, ...]], None]) -> None:
        from gi.repository import Gio

        try:
            bus = Gio.bus_get_sync(bus_type, None)
        except Exception as e:  # noqa: BLE001 - no such bus: the events never come
            log_event("session_bus_unavailable", signal=signal, kind=type(e).__name__)
            return

        def on_signal(_bus: Any, _sender: Any, _path: Any, _iface: Any, _name: Any,
                      params: Any) -> None:
            try:
                handler(tuple(params.unpack()))
            except Exception as e:  # noqa: BLE001 - one bad event must not stop the next
                log_event("session_event_error", signal=signal, kind=type(e).__name__)

        # No sender filter: the bus policy decides who may emit these interfaces, and a
        # private test bus has no logind to name.
        bus.signal_subscribe(None, iface, signal, None, None, Gio.DBusSignalFlags.NONE,
                             on_signal)
        self._buses.append(bus)
