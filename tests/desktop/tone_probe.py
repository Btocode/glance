"""Drives the tone part of a GtkOverlay panel on $DISPLAY and prints what happened as JSON.

    tone_probe.py [--screenshot PATH]

Run by tests/desktop/test_panel_tone.py against a private Xvfb server. It moves the pointer
and clicks with XTEST, so never point it at a display somebody is using.
"""
import argparse
import json
import sys
import time

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk
from Xlib import X, display
from Xlib.ext import xtest

from glance.diffing import word_diff
from glance.ui.overlay_gtk import HOVER_MS, GtkOverlay, button_text
from glance.ui.panel_model import PanelView, ToneChip

OLD = "ok send me the report today, i need it before the <b>meeting</b> starts & ends."
NEW = "Could you send me the report today? I need it before the <b>meeting</b> starts & ends."
CHIPS = (ToneChip("friendlier", "Friendlier", 1), ToneChip("more_direct", "More direct", 2),
         ToneChip("shorter", "Shorter", 3), ToneChip("professional", "Professional", 4),
         ToneChip("custom", "Rewrite as…", None))
PROFILES = (("team_channel", "Team channel"), ("dm", "Direct message"), ("email", "Email"))


def pump(seconds: float) -> None:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.005)


def find_button(widget: Gtk.Widget, label: str) -> Gtk.Button | None:
    if isinstance(widget, Gtk.Button) and (button_text(widget).startswith(label)
                                           or label in button_text(widget).split(" | ")):
        return widget
    if isinstance(widget, Gtk.Container):
        for child in widget.get_children():
            found = find_button(child, label)
            if found is not None:
                return found
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--screenshot")
    args = ap.parse_args()

    events: list = []
    overlay = GtkOverlay(lambda: events.append(["chip_click"]),
                         lambda action, a: events.append([action, a]))
    d = display.Display()
    root = d.screen().root
    holder = root.create_window(0, 0, 60, 60, 0, d.screen().root_depth)
    holder.map()
    d.sync()
    pump(0.2)
    holder.set_input_focus(X.RevertToParent, X.CurrentTime)
    d.sync()

    def focused() -> bool:
        return d.get_input_focus().focus.id == holder.id

    def centre(label: str) -> tuple[int, int]:
        button = find_button(overlay.panel_window, label)
        assert button is not None, label
        px, py = overlay.panel_window.get_position()
        bx, by = button.translate_coordinates(overlay.panel_window, 0, 0)
        alloc = button.get_allocation()
        return px + bx + alloc.width // 2, py + by + alloc.height // 2

    def move(label: str, rest_s: float) -> None:
        x, y = centre(label)
        xtest.fake_input(d, X.MotionNotify, x=x, y=y)
        d.sync()
        pump(rest_s)

    def click(label: str) -> None:
        move(label, 0.05)
        xtest.fake_input(d, X.ButtonPress, 1)
        xtest.fake_input(d, X.ButtonRelease, 1)
        d.sync()
        pump(0.25)

    def labels() -> list[str]:
        found: list[str] = []

        def walk(widget: Gtk.Widget) -> None:
            if isinstance(widget, Gtk.Button):
                found.append(button_text(widget))
            elif isinstance(widget, Gtk.Container):
                for child in widget.get_children():
                    walk(child)

        walk(overlay.panel_window)
        return found

    out: dict = {"focus_before": focused()}
    base = PanelView(tone="Blunt", tone_reason="curt for a channel", tone_chips=CHIPS,
                     profile_label="Team channel", profiles=PROFILES)

    # 1. chips only: hovering asks for a preview, but never for "Rewrite as…"
    overlay.show_panel(base, 300, 150)
    pump(0.4)
    out["chips_only"] = labels()
    move("Close", 0.1)
    move("Shorter", HOVER_MS / 1000 / 3)  # passing over it is not resting on it
    move("Close", HOVER_MS / 1000 + 0.2)
    out["after_passing_over"] = list(events)
    move("Shorter", HOVER_MS / 1000 + 0.3)
    move("Rewrite as…", HOVER_MS / 1000 + 0.3)
    move("Close", 0.1)
    out["after_resting"] = list(events)
    events.clear()

    # 2. loading, then a streamed and a finished preview
    overlay.show_panel(PanelView(**{**base.__dict__, "preview_state": "loading"}), 300, 150)
    pump(0.3)
    out["loading"] = labels()
    ready = PanelView(**{**base.__dict__, "preview": word_diff(OLD, NEW),
                         "preview_state": "ready", "can_apply": True})
    overlay.show_panel(ready, 300, 150)
    pump(0.4)
    out["ready"] = labels()
    out["ready_size"] = list(overlay.panel_window.get_size())
    if args.screenshot:
        w, h = d.screen().width_in_pixels, d.screen().height_in_pixels
        Gdk.pixbuf_get_from_window(Gdk.get_default_root_window(), 0, 0, w, h).savev(
            args.screenshot, "png", [], [])
    click("Shorter")
    click("Rewrite as…")
    click("For Team channel")  # opens the audience list
    out["profiles_open"] = labels()
    click("Email")
    click("For Team channel")  # and closes it again
    out["profiles_closed"] = labels()
    out["clicks"] = list(events)
    events.clear()

    # 3. a rewrite that may only be copied; a very long one still fits the panel
    long_new = " ".join(f"word{i}" for i in range(900))
    blocked = PanelView(**{**base.__dict__, "preview": word_diff(OLD, long_new),
                           "preview_state": "ready", "can_apply": False,
                           "copy_offer": long_new, "banner": "Copy the rewrite instead."})
    overlay.show_panel(blocked, 300, 150)
    pump(0.5)
    out["blocked"] = labels()
    out["blocked_size"] = list(overlay.panel_window.get_size())
    click("Copy rewrite")
    out["blocked_clicks"] = list(events)

    out["focus_after"] = focused()
    out["panel_active"] = overlay.panel_window.is_active()
    overlay.hide_panel()
    pump(0.1)
    sys.stdout.write(json.dumps(out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
