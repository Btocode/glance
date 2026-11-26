"""Only Glance may use the extension: any other process on the session bus is refused.

Same rules as test_wayland_e2e.py (private headless GNOME Shell, GLANCE_WAYLAND_TEST=1).
This test process is such an "other process": it is neither glanced nor glance-capture.
"""
from __future__ import annotations

import time

import pytest

from glance.shellbus import ShellUnavailable
from tests.wayland.harness import Glance, GtkApp, Shell, private_root, wait_for
from tests.wayland.test_wayland_e2e import pytestmark  # noqa: F401

TEXT = "We needs a plan for this week and it is fine."


@pytest.fixture(scope="module")
def desk():
    root = private_root()
    assert root is not None
    shell = Shell(root).start()
    app = glance = None
    try:
        app = GtkApp(shell, TEXT, "--undo")
        glance = Glance(shell, root, name="glanced-access")
        assert wait_for(lambda: glance.status().get("shell_extension") == "running", 20)
        yield shell, app, glance
    finally:
        for part in (glance, app):
            if part is not None:
                part.stop()
        shell.stop()


def refused(shell: Shell, method: str, payload: dict) -> bool:
    try:
        shell.call_directly(method, payload)
    except ShellUnavailable:
        return True
    return False


def test_a_foreign_client_cannot_type_read_or_render(desk):
    shell, app, _glance = desk
    # glanced, verified by the extension, works (its icon is up for the field)
    assert wait_for(lambda: (o := shell.overlay()).get("status") and o, 30)
    assert shell.call_directly("Ping", {})["ok"]  # the one open method
    shell.call("ClipboardSet", {"which": "primary", "text": "secret selection"})
    for method, payload in [
        ("TypeText", {"text": "INJECTED"}),
        ("Chord", {"keys": ["ctrl", "a"]}),
        ("ReadPrimary", {"max_bytes": 1000}),
        ("ClipboardSave", {"which": "clipboard"}),
        ("ClipboardSet", {"which": "clipboard", "text": "x"}),
        ("FocusedWindow", {}),
        ("KeyState", {}),
        ("Render", {"status": None}),
        ("Grab", {"name": "panel", "accel": "<Super>x"}),
        ("Ask", {"prompt": "?"}),
    ]:
        assert refused(shell, method, payload), method
    time.sleep(0.5)
    assert app.buffer() == TEXT  # nothing was typed
    assert shell.overlay().get("status")  # nothing was hidden


def test_a_squatter_on_glances_name_is_not_trusted(desk):
    shell, _app, _glance = desk
    from gi.repository import GLib

    conn = shell._bus.connection
    # take glance-capture's name from this (non-Glance) process
    reply = conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                           "org.freedesktop.DBus", "RequestName",
                           GLib.Variant("(su)", ("io.github.btocode.GlanceCapture", 2)),
                           None, 0, 2000, None)
    assert reply.unpack()[0] in (1, 2, 3, 4)
    try:
        assert refused(shell, "TypeText", {"text": "INJECTED"})
        assert refused(shell, "FocusedWindow", {})
    finally:
        conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                       "org.freedesktop.DBus", "ReleaseName",
                       GLib.Variant("(s)", ("io.github.btocode.GlanceCapture",)),
                       None, 0, 2000, None)
