"""Power state from UPower on the system bus."""
from __future__ import annotations

from collections.abc import Callable

TIMEOUT_MS = 500


def _read_upower() -> object:
    from gi.repository import Gio, GLib

    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    reply = bus.call_sync(
        "org.freedesktop.UPower", "/org/freedesktop/UPower",
        "org.freedesktop.DBus.Properties", "Get",
        GLib.Variant("(ss)", ("org.freedesktop.UPower", "OnBattery")),
        GLib.VariantType("(v)"), Gio.DBusCallFlags.NO_AUTO_START, TIMEOUT_MS, None,
    )
    return reply.unpack()[0]


def on_battery(read: Callable[[], object] = _read_upower) -> bool:
    """True only when UPower says so; False on any error (no UPower, no bus, timeout)."""
    try:
        return read() is True
    except Exception:  # noqa: BLE001 - a desktop without UPower must not affect the daemon
        return False
