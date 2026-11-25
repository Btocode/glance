"""Drives a GtkOverlay on whatever $DISPLAY is and prints what happened as one JSON line.

    overlay_probe.py [--shots DIR]

Run by tests/test_xvfb.py against a private Xvfb server. It clicks with XTEST, so never
point it at a display somebody is using. With --shots it saves the collapsed strip and the
open drawer, light and dark, as PNGs in DIR.
"""
import argparse
import json
import re
import sys
import time
from dataclasses import replace
from pathlib import Path

import cairo
import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk
from Xlib import X, display
from Xlib.ext import xtest

from glance.grammar.issues import Issue
from glance.protocol import Rect
from glance.ui.chip_model import ChipState, ChipView
from glance.ui.overlay_gtk import GAP, GtkOverlay, button_text
from glance.ui.panel_model import PanelView, ToneChip, build_panel
from glance.ui.placement import Anchor
from glance.ui.strip_model import StripView, strip_fix

TYPED = "hey team i think its fine to ship friday but we needs the tests fixed asap"


def _issue(word: str, repl: str, rule: str, safe: bool = True, alts: tuple = ()) -> Issue:
    start = re.search(rf"\b{re.escape(word)}\b", TYPED).start()
    end = start + len(word)
    return Issue(f"harper:{rule}:{start}:{end}", "harper", rule, f"{rule} problem", start, end,
                 (repl, *alts), safe)


# What harper-ls (and an analysis) would say about TYPED: the strip and the drawer's fix line
# are both made from these, as the daemon makes them.
ISSUES = [_issue("hey", "Hey", "SentenceCapitalization"),
          _issue("i", "I", "CapitalizePersonalPronouns"),
          _issue("its", "it's", "ItsContraction"),
          _issue("friday", "Friday,", "SpellCheck", safe=False, alts=("Fridays",)),
          _issue("needs", "need", "PronounVerbAgreement"),
          _issue("asap", "ASAP.", "AsSoonAsPossible")]
_SAFE = [replace(i, safe=True) for i in ISSUES]  # the "Friday," fix came with an analysis
_FIX = strip_fix(TYPED, len(TYPED), _SAFE)
STRIP = StripView(_FIX.segments, _FIX.n_fixes)
ANCHOR = Anchor(caret=Rect(330, 300, 1, 18), field=Rect(300, 270, 660, 70))
CHIPS = (ToneChip("friendlier", "Friendlier", 1, "Could someone pick this up? Thanks!",
                  "ready"),
         ToneChip("more_direct", "More direct", 2, "Ship Friday once tests pass.", "ready"),
         ToneChip("shorter", "Shorter", 3), ToneChip("professional", "Professional", 4,
                                                     None, "loading"),
         ToneChip("custom", "Rewrite as…", None))
ROWS = build_panel(TYPED, ISSUES).issues
DRAWER = PanelView(tone="Blunt", tone_reason="curt for a channel", tone_chips=CHIPS,
                   issues=ROWS, can_fix_all=True, profile_label="Team channel",
                   profiles=(("team_channel", "Team channel"), ("email", "Email")))


def pump(seconds: float) -> None:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.005)


def buttons(widget: Gtk.Widget) -> list[Gtk.Button]:
    if isinstance(widget, Gtk.Button):
        return [widget]
    if isinstance(widget, Gtk.Container):
        return [b for c in widget.get_children() for b in buttons(c)]
    return []


def walk(widget: Gtk.Widget) -> list[Gtk.Widget]:
    out = [widget]
    if isinstance(widget, Gtk.Container):
        for c in widget.get_children():
            out += walk(c)
    return out


def find(window: Gtk.Window, text: str) -> Gtk.Button | None:
    for b in buttons(window):
        if button_text(b) == text or button_text(b).startswith(text + " |"):
            return b
    return None


class scenery:
    """A stand-in for the app on screen (for the screenshots): a page and the message box
    with the user's text in it, where ANCHOR says the field is. It takes no clicks."""

    def __init__(self) -> None:
        self.css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), self.css, 900)
        self.page = Gtk.Window(type=Gtk.WindowType.POPUP)
        self.page.move(0, 0)
        self.page.set_size_request(1280, 800)
        self.page.set_name("page")
        self.field = Gtk.Window(type=Gtk.WindowType.POPUP)
        f = ANCHOR.field
        self.field.move(f.x, f.y)
        self.field.set_size_request(f.w, f.h)
        self.field.set_name("field")
        label = Gtk.Label(label=TYPED, xalign=0, wrap=True, max_width_chars=90)
        label.set_margin_start(30)
        label.set_margin_top(20)
        label.set_valign(Gtk.Align.START)
        self.field.add(label)
        self.dark(False)
        for w in (self.page, self.field):
            w.show_all()  # above the focus holder, below the overlay: clicks pass through
            w.get_window().input_shape_combine_region(cairo.Region(), 0, 0)

    def dark(self, on: bool) -> None:
        page, box, ink, line = (("#2b2e2d", "#1e2120", "#e8ecea", "#3a403e") if on else
                                ("#f4f5f2", "#ffffff", "#17191c", "#dfe3de"))
        self.css.load_from_data(f"""
            #page {{ background-color: {page}; }}
            #field {{ background-color: {box}; border: 1px solid {line}; }}
            #field label {{ color: {ink}; font-size: 15px; }}""".encode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots")
    args = ap.parse_args()

    events: list = []
    overlay = GtkOverlay(lambda: events.append(["chip_click"]),
                         lambda action, a: events.append([action, a]))
    d = display.Display()
    root = d.screen().root
    # The app being written in: it holds the input focus and sits under everything, so a
    # click that goes through the strip lands on it.
    holder = root.create_window(0, 0, 1280, 800, 0, d.screen().root_depth,
                                event_mask=X.ButtonPressMask,
                                background_pixel=d.screen().white_pixel)
    holder.map()
    d.sync()
    pump(0.2)
    holder.set_input_focus(X.RevertToParent, X.CurrentTime)
    d.sync()

    def focused() -> bool:
        return d.get_input_focus().focus.id == holder.id

    def holder_clicks() -> int:
        n = 0
        while d.pending_events():
            if d.next_event().type == X.ButtonPress:
                n += 1
        return n

    def click(x: int, y: int) -> None:
        xtest.fake_input(d, X.MotionNotify, x=x, y=y)
        xtest.fake_input(d, X.ButtonPress, 1)
        xtest.fake_input(d, X.ButtonRelease, 1)
        d.sync()
        pump(0.3)

    def press(window: Gtk.Window, text: str) -> bool:
        b = find(window, text)
        if b is None:
            out.setdefault("missing_buttons", []).append(text)
            return False
        wx, wy = window.get_position()
        bx, by = b.translate_coordinates(window, 0, 0)
        a = b.get_allocation()
        click(wx + bx + a.width // 2, wy + by + a.height // 2)
        return True

    def override_redirect(window: Gtk.Window) -> bool:
        xid = window.get_window().get_xid()
        return bool(d.create_resource_object("window", xid).get_attributes().override_redirect)

    def real(window: Gtk.Window) -> list[int]:
        return [*window.get_position(), *window.get_size()]

    def shot(name: str) -> None:
        if args.shots:
            g = overlay.strip_geometry() or (0, 0, 1, 1)
            p = overlay.panel_geometry()
            x0, y0 = g[0] - 40, g[1] - 70
            x1 = max(g[0] + g[2], p[0] + p[2] if p else 0) + 40
            y1 = max(g[1] + g[3], p[1] + p[3] if p else 0) + 30
            pix = Gdk.pixbuf_get_from_window(Gdk.get_default_root_window(), x0, y0,
                                             x1 - x0, y1 - y0)
            pix.savev(str(Path(args.shots) / f"{name}.png"), "png", [], [])

    overlay._theme.force(False)
    page = scenery()
    out: dict = {"focus_before": focused(),
                 "monitors": [[m.x, m.y, m.w, m.h] for m in overlay.monitors()]}

    # ---- the status dot: only for the quiet states
    overlay.show_chip(ChipView(ChipState.FIXES, "3", None), 300, 200)
    pump(0.2)
    out["dot_for_fixes"] = overlay.chip_geometry()
    overlay.show_chip(ChipView(ChipState.PAUSED, "Paused", None), 300, 200)
    pump(0.3)
    out["dot"] = real(overlay.chip_window)
    out["dot_reported"] = list(overlay.chip_geometry())
    out["dot_override_redirect"] = override_redirect(overlay.chip_window)
    click(307, 207)
    out["focus_after_dot_click"] = focused()
    overlay.hide_chip()
    holder_clicks()

    # ---- the collapsed strip
    overlay.show_strip(STRIP, ANCHOR)
    pump(0.4)
    out["strip"] = real(overlay.strip_window)
    out["strip_reported"] = list(overlay.strip_geometry())
    out["strip_text"] = overlay.strip_text()
    out["strip_buttons"] = [button_text(b) for b in buttons(overlay.strip_window)]
    out["strip_override_redirect"] = override_redirect(overlay.strip_window)
    out["focusable"] = [[w.get_accept_focus(), w.get_focus_on_map(), w.is_active()]
                        for w in (overlay.chip_window, overlay.strip_window,
                                  overlay.panel_window)]
    # theme: light unless told otherwise
    shot("strip-collapsed-light")
    sx, sy, _sw, sh = overlay.strip_geometry()
    holder_clicks()
    click(sx + 60, sy + sh // 2)  # on the sentence: not a button, so it goes through
    out["click_through_sentence"] = holder_clicks()
    press(overlay.strip_window, "Use  Alt+⏎")
    press(overlay.strip_window, "⋯")
    out["clicks_on_strip_reach_holder"] = holder_clicks()
    out["focus_after_strip_clicks"] = focused()
    out["strip_events"] = list(events)
    events.clear()

    moves = []
    xid = overlay.strip_window.get_window().get_xid()
    xwin = d.create_resource_object("window", xid)
    for i in range(20):
        t0 = time.monotonic()
        overlay.show_strip(STRIP, ANCHOR.moved(7 * (i + 1), 0))
        while xwin.get_geometry().x != 300 + 7 * (i + 1):
            pump(0.002)
            if time.monotonic() - t0 > 1:
                break
        moves.append(time.monotonic() - t0)
    out["move_median_s"] = sorted(moves)[len(moves) // 2]

    low = Anchor(caret=Rect(330, 770, 1, 18), field=Rect(300, 750, 560, 40))
    overlay.show_strip(STRIP, low)
    pump(0.3)
    out["strip_low"] = list(overlay.strip_geometry())
    overlay.show_strip(StripView(note="Reads blunt for team channel — ⋯ for tone options"),
                       ANCHOR)
    pump(0.3)
    out["note_buttons"] = [button_text(b) for b in buttons(overlay.strip_window)]
    out["note_text"] = overlay.strip_text()

    # ---- the drawer under it
    overlay.show_strip(StripView(STRIP.segments, STRIP.n_fixes, expanded=True), ANCHOR)
    overlay.show_panel(DRAWER, 0, 0, anchor=ANCHOR)
    pump(0.5)
    out["strip_open"] = list(overlay.strip_geometry())
    out["panel"] = real(overlay.panel_window)
    out["panel_reported"] = list(overlay.panel_geometry())
    out["panel_override_redirect"] = override_redirect(overlay.panel_window)
    out["focus_with_panel"] = focused()
    out["drawer_buttons"] = [button_text(b) for b in buttons(overlay.panel_window)]
    shot("strip-expanded-light")
    for text in ("Shorter", "Rewrite as…", "Fix all", "hey → Hey", "its → it's"):
        press(overlay.panel_window, {"Rewrite as…": "e.g. make it a bullet list"}.get(
            text, text))
    press(overlay.panel_window, "6 fixes")  # every fix, with its other choices
    pump(0.3)
    out["details_buttons"] = [button_text(b) for b in buttons(overlay.panel_window)]
    out["panel_details"] = real(overlay.panel_window)
    press(overlay.panel_window, "Ignore here")  # the first fix's
    for w in walk(overlay.panel_window):  # down to "friday", the one spelling choice
        if isinstance(w, Gtk.ScrolledWindow):
            adj = w.get_vadjustment()
            adj.set_value(adj.get_upper())
    pump(0.3)
    for text in ("Add to dictionary", "Fridays", "For Team channel ▾"):
        press(overlay.panel_window, text)
    press(overlay.panel_window, "Email")
    press(overlay.panel_window, "Close")
    out["focus_after_panel_clicks"] = focused()
    out["active_after_clicks"] = [overlay.strip_window.is_active(),
                                  overlay.panel_window.is_active()]
    out["drawer_events"] = list(events)
    events.clear()

    # ---- dark
    overlay._theme.force(True)
    page.dark(True)
    pump(0.5)
    out["dark"] = overlay._theme.dark
    overlay.hide_panel()
    overlay.show_strip(STRIP, ANCHOR)
    pump(0.4)
    shot("strip-collapsed-dark")
    overlay.show_strip(StripView(STRIP.segments, STRIP.n_fixes, expanded=True), ANCHOR)
    overlay.show_panel(DRAWER, 0, 0, anchor=ANCHOR)
    pump(0.5)
    out["dark_panel"] = overlay.panel_geometry()
    out["dark_strip"] = overlay.strip_geometry()
    shot("strip-expanded-dark")
    overlay._theme.force(False)
    page.dark(False)
    pump(0.3)

    # ---- select-anything's panel, at a point
    view = PanelView(mode="selection", tone_chips=CHIPS[:4], issues=ROWS[:2],
                     banner="Copy the rewrite instead.", can_paste=True,
                     profile_label="Email")
    overlay.hide_panel()
    overlay.show_panel(view, 5000, 5000)
    pump(0.4)
    out["selection"] = real(overlay.panel_window)
    out["selection_buttons"] = [button_text(b) for b in buttons(overlay.panel_window)]
    shot("selection-light")
    overlay.show_panel(PanelView(), 100, 100, anchor=ANCHOR)  # nothing at all to say
    pump(0.3)
    out["empty_panel"] = real(overlay.panel_window)

    overlay.copy_to_clipboard("copied by the probe")
    pump(0.1)
    out["clipboard"] = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).wait_for_text()

    overlay.hide_chip()
    overlay.hide_strip()
    overlay.hide_panel()
    pump(0.2)
    out["hidden"] = [overlay.chip_geometry(), overlay.strip_geometry(),
                     overlay.panel_geometry(), overlay.strip_window.get_visible(),
                     overlay.panel_window.get_visible()]
    out["events"] = events
    out["consts"] = {"gap": GAP, "shadow": list(overlay._shadow)}
    sys.stdout.write(json.dumps(out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
