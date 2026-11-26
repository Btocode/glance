"""The overlay next to a real focused window. Run inside the GNOME session:

    .venv/bin/pytest -m desktop tests/desktop/test_overlay.py -v

The tests open their own window, give it the focus and show the status icon and the panel beside
it for a moment. They send no keys and no clicks. (The same windows are exercised with
clicks, on a private Xvfb server, by tests/test_xvfb.py in the default run.)
"""
from __future__ import annotations

import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from glance.protocol import Rect
from glance.ui.panel_model import IssueRow, PanelView
from glance.ui.placement import Anchor
from glance.ui.status_model import StatusState, StatusView
from tests.desktop.test_capture_gtk import Desktop, Lines, _stop
from tests.desktop.test_writeback_gtk import _pump

pytestmark = pytest.mark.desktop

APP = Path(__file__).with_name("gtk_app.py")
TITLE = "glance-test-app"
FIXES = StatusView(StatusState.READY, 2)
PANEL = PanelView(
    issues=(IssueRow("harper:Typo:0:3", "Teh quick brown fox", "Did you mean The?", ("The",),
                     "Typo"),),
    can_fix_all=True)


def _active_title() -> str:
    import gi

    gi.require_version("Wnck", "3.0")
    from gi.repository import Wnck

    screen = Wnck.Screen.get_default()
    screen.force_update()
    window = screen.get_active_window()
    return window.get_name() if window is not None else ""


@pytest.fixture
def stage():
    if not os.environ.get("DISPLAY"):
        pytest.skip("no X display")
    from glance.capture.atspi_port import AtspiPort
    from glance.ui.overlay_gtk import GtkOverlay

    app = subprocess.Popen([sys.executable, str(APP), "--text", "Teh quick brown fox"],
                           stdout=subprocess.PIPE, text=True)
    port = None
    overlay = GtkOverlay(lambda: None, lambda action, args: None)
    try:
        assert Lines(app).wait_for(lambda line: line == "READY", timeout=10), "app not ready"
        desktop = Desktop()
        _pump(0.3)
        assert desktop.activate(app.pid), "could not give the test window focus"
        port = AtspiPort()
        _pump(0.4)
        field = port.focused()
        assert field is not None, "the test window's text view is not focused"
        caret = field.read().caret_rect
        assert caret is not None
        assert _active_title() == TITLE
        yield SimpleNamespace(overlay=overlay, desktop=desktop, app=app,
                              x=caret.x + caret.w + 4, y=caret.y + caret.h + 4,
                              at=lambda x, y: Anchor(caret=Rect(x, y, 1, caret.h)))
    finally:
        overlay.hide_status()
        overlay.hide_panel()
        if port is not None:
            port.stop()
        _stop(app)
        _pump(0.4)


def test_status_icon_does_not_take_focus(stage):
    stage.overlay.show_status(FIXES, stage.at(stage.x, stage.y))
    _pump(0.5)
    assert stage.overlay.status_geometry() is not None
    assert _active_title() == TITLE
    assert stage.desktop.active_pid() == stage.app.pid


def test_panel_does_not_take_focus(stage):
    stage.overlay.show_status(FIXES, stage.at(stage.x, stage.y))
    stage.overlay.show_panel(PANEL, stage.x, stage.y + 26)
    _pump(0.5)
    assert stage.overlay.panel_geometry() is not None
    assert _active_title() == TITLE
    assert stage.desktop.active_pid() == stage.app.pid
    assert not stage.overlay.panel_window.is_active()
    assert not stage.overlay.status_window.is_active()


def test_status_icon_reposition_under_50ms(stage):
    from Xlib import display

    d = display.Display()
    try:
        stage.overlay.show_status(FIXES, stage.at(stage.x, stage.y))
        _pump(0.3)
        x0 = stage.overlay.status_geometry()[0]
        xid = stage.overlay.status_window.get_window().get_xid()
        window = d.create_resource_object("window", xid)
        times = []
        for i in range(100):
            dx = (i % 10) * 9 + 1
            want = x0 + dx - 1  # the window starts 1 px left of the circle
            t0 = time.monotonic()
            stage.overlay.show_status(FIXES, stage.at(stage.x + dx, stage.y))
            while window.get_geometry().x != want and time.monotonic() - t0 < 1.0:
                _pump(0.002)
            times.append(time.monotonic() - t0)
        assert statistics.median(times) < 0.05
        assert _active_title() == TITLE
    finally:
        d.close()
