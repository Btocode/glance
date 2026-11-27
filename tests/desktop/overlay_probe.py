"""Drives a GtkOverlay on whatever $DISPLAY is and prints what happened as one JSON line.

    overlay_probe.py [--shots DIR]

Run by tests/test_xvfb.py against a private Xvfb server. It clicks with XTEST, so never
point it at a display somebody is using. With --shots it saves every piece in each state, light and dark, as
v3-<state>-<theme>.png in DIR: the status icon (idle, working, ready, tone, muted), the strip,
the strip a muted icon opens, the icon's toolbar (and muted), a tone's preview in the
strip, the assistant panel, select-anything's panel,
the ghost text and the "Keyboard shortcuts" window.
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

from glance.config import GeneralCfg
from glance.diffing import word_diff
from glance.grammar.issues import Issue
from glance.protocol import Rect
from glance.ui.ghost_gtk import GtkGhost
from glance.ui.icon_toolbar import icon_toolbar
from glance.ui.overlay_gtk import GAP, GtkOverlay, button_text
from glance.ui.panel_model import PanelView, ToneChip, build_panel
from glance.ui.placement import Anchor
from glance.ui.shortcut_table import glance_shortcuts
from glance.ui.shortcuts_gtk import ShortcutsWindow
from glance.ui.status_model import NO_AI_LINE, PAUSED_LINE, StatusState, StatusView
from glance.ui.strip_model import Segment, StripView, strip_fix

TYPED = "hey team i think its fine to ship friday but we needs the tests fixed asap"
GHOST_TYPED = "Hey team, I think it's fine to ship Friday"  # the ghost text's shot


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
SELECTION = PanelView(mode="selection", tone_chips=CHIPS, issues=ROWS[:2],
                      banner="Copy the rewrite instead.", can_paste=True,
                      profile_label="Email")
DRAWER = PanelView(tone="Blunt", tone_reason="curt for a channel", tone_chips=CHIPS,
                   issues=ROWS, can_fix_all=True, profile_label="Team channel",
                   profiles=(("team_channel", "Team channel"), ("email", "Email")))
FRIENDLIER = "Hey team! I think it's fine to ship Friday, but could we get the tests fixed soon?"
# "Friendlier" on the icon's toolbar: its rewrite previewed in the strip, Use applies it.
_OPS = word_diff(TYPED, FRIENDLIER)
TONE_STRIP = StripView(tuple(Segment(o.text, o.kind == "ins") for o in _OPS if o.kind != "del"),
                       3, on_demand=True, label="Friendlier")
# A click on "Friendlier": selected, its rewrite in "Suggested", Apply types it.
SELECTED = replace(
    DRAWER, tone_chips=(replace(CHIPS[0], preview=FRIENDLIER, selected=True), *CHIPS[1:]),
    preview=word_diff(TYPED, FRIENDLIER), preview_state="ready", can_apply=True,
    preview_label="Friendlier")
# ... and "Professional" a moment after its click: being written.
WORKING = replace(
    DRAWER, tone_chips=(*CHIPS[:3], replace(CHIPS[3], selected=True), CHIPS[4]),
    preview=word_diff(TYPED, "Hi team, I believe it is fine to ship"),
    preview_state="loading", preview_label="Professional")


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
        parts = button_text(b).split(" | ")
        if button_text(b) == text or parts[0] == text or parts[-1] == text:
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
        self.label = label
        self.field.add(label)
        self.dark(False)
        for w in (self.page, self.field):
            w.show_all()  # above the focus holder, below the overlay: clicks pass through
            w.get_window().input_shape_combine_region(cairo.Region(), 0, 0)

    def text_end(self) -> tuple[int, int, int]:
        """(x, y, h) on the screen of the caret after the field's last character."""
        layout = self.label.get_layout()
        ox, oy = self.label.get_layout_offsets()
        pos = layout.index_to_pos(len(self.label.get_text().encode()))
        f = ANCHOR.field
        k = 1024  # Pango units
        return (f.x + ox + (pos.x + pos.width) // k, f.y + oy + pos.y // k, pos.height // k)

    def dark(self, on: bool) -> None:
        page, box, ink, line = (("#0f141a", "#1b222c", "#e6edf3", "#2a3340") if on else
                                ("#eef1f4", "#ffffff", "#17191c", "#d8dee4"))
        self.css.load_from_data(f"""
            #page {{ background-color: {page}; }}
            #field {{ background-color: {box}; border: 1px solid {line}; }}
            #field label {{ color: {ink}; font-size: 15px; }}""".encode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots")
    args = ap.parse_args()

    events: list = []
    # what the icon's toolbar is made from: the icon's state (muted for one screenshot)
    bar_for = {"view": StatusView(StatusState.IDLE)}
    overlay = GtkOverlay(lambda: events.append(["chip_click"]),
                         lambda action, a: events.append([action, a]),
                         icon_toolbar=lambda on_purpose=False: icon_toolbar(bar_for["view"],
                                                                            "Super+Alt+G"))
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

    def grab(name: str, rects: list, pad: int = 24) -> None:
        """The screen around `rects` ((x, y, w, h) each) as DIR/<name>.png."""
        if not args.shots:
            return
        rects = [r for r in rects if r]
        x0 = max(0, min(r[0] for r in rects) - pad)
        y0 = max(0, min(r[1] for r in rects) - pad)
        x1 = min(1280, max(r[0] + r[2] for r in rects) + pad)
        y1 = min(800, max(r[1] + r[3] for r in rects) + pad)
        icon = overlay.status_window
        drawn = icon.get_visible() and not overlay._composited
        if drawn:
            # Xvfb has no compositor, so the icon's window is cut to its mark there. Shoot
            # what is under it and draw the icon on that as a compositor shows it: its own
            # draw function onto the picture (glow and soft edges included).
            ix, iy = icon.get_position()
            icon.hide()
            pump(0.15)
        pix = Gdk.pixbuf_get_from_window(Gdk.get_default_root_window(), x0, y0,
                                         x1 - x0, y1 - y0)
        path = str(Path(args.shots) / f"{name}.png")
        if not drawn:
            pix.savev(path, "png", [], [])
            return
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, x1 - x0, y1 - y0)
        cr = cairo.Context(surface)
        Gdk.cairo_set_source_pixbuf(cr, pix, 0, 0)
        cr.paint()
        cr.translate(ix - x0, iy - y0)
        cr.push_group()
        overlay._composited = True
        overlay._draw_icon(icon, cr)
        overlay._composited = False
        cr.pop_group_to_source()
        cr.paint()
        surface.write_to_png(path)
        icon.show()
        pump(0.15)

    def field() -> tuple:
        f = ANCHOR.field
        return (f.x, f.y, f.w, f.h)

    def shot(name: str) -> None:
        """The field and whatever of the overlay is up under it."""
        xtest.fake_input(d, X.MotionNotify, x=5, y=5)  # no button hovered
        d.sync()
        pump(0.2)
        grab(name, [field(), overlay.strip_geometry(), overlay.panel_geometry()])

    overlay._theme.force(False)
    page = scenery()
    out: dict = {"focus_before": focused(),
                 "monitors": [[m.x, m.y, m.w, m.h] for m in overlay.monitors()]}

    # ---- the status icon
    overlay.show_chip(None, 300, 200)  # the chip's state draws nothing any more
    pump(0.1)
    overlay.show_status(StatusView(StatusState.IDLE), ANCHOR)
    pump(0.3)
    out["icon"] = list(overlay.status_geometry())
    out["icon_window"] = real(overlay.status_window)
    out["icon_override_redirect"] = override_redirect(overlay.status_window)
    out["icon_idle"] = overlay.status_state()
    ix, iy, iw, ih = overlay.status_geometry()
    holder_clicks()
    click(ix + iw // 2, iy + ih // 2)
    out["icon_click_events"] = list(events)
    out["icon_click_holder"] = holder_clicks()
    out["focus_after_icon_click"] = focused()
    wx, wy, ww, wh = out["icon_window"]
    click(wx + ww - 2, wy + wh - 2)  # inside its window, outside the circle: goes through
    out["icon_corner_click_holder"] = holder_clicks()
    # the toolbar: the pointer resting on the icon opens nothing (right-click only),
    # a right-click opens it and a click on an item runs it
    xtest.fake_input(d, X.MotionNotify, x=ix + iw // 2, y=iy + ih // 2)
    d.sync()
    pump(0.6)
    out["hover_items"] = overlay.toolbar_items()
    xtest.fake_input(d, X.ButtonPress, 3)
    xtest.fake_input(d, X.ButtonRelease, 3)
    d.sync()
    pump(0.3)
    out["menu_items"] = overlay.toolbar_items()
    out["menu_geometry"] = overlay.toolbar_geometry()
    out["caret_bottom"] = ANCHOR.caret.y + ANCHOR.caret.h
    out["menu_override_redirect"] = override_redirect(overlay._menu) \
        if overlay._menu.get_visible() else None
    out["focus_with_menu"] = focused()
    events.clear()
    press(overlay._menu, "Check")
    out["menu_click_events"] = list(events)
    out["menu_after_click"] = overlay.toolbar_items()
    out["focus_after_menu_click"] = focused()
    # a right-click on the icon opens it at once; another closes it
    xtest.fake_input(d, X.MotionNotify, x=5, y=5)
    d.sync()
    pump(0.6)
    xtest.fake_input(d, X.MotionNotify, x=ix + iw // 2, y=iy + ih // 2)
    xtest.fake_input(d, X.ButtonPress, 3)
    xtest.fake_input(d, X.ButtonRelease, 3)
    d.sync()
    pump(0.15)  # sooner than the hover would
    out["rightclick_items"] = overlay.toolbar_items()
    out["rightclick_events"] = list(events)
    xtest.fake_input(d, X.ButtonPress, 3)
    xtest.fake_input(d, X.ButtonRelease, 3)
    d.sync()
    pump(0.15)
    out["rightclick_again"] = overlay.toolbar_items()
    out["focus_after_rightclick"] = focused()
    xtest.fake_input(d, X.MotionNotify, x=5, y=5)
    d.sync()
    pump(0.6)
    holder_clicks()
    events.clear()
    overlay.show_status(StatusView(StatusState.WORKING, spin=True), ANCHOR)
    pump(0.3)
    out["icon_working"] = overlay.status_state()
    overlay.show_status(StatusView(StatusState.READY, 3), ANCHOR)
    pump(0.2)
    out["icon_ready"] = overlay.status_state()
    overlay.show_status(StatusView(StatusState.MUTED, message=NO_AI_LINE), ANCHOR)
    pump(0.2)
    out["icon_muted"] = overlay.status_state()
    overlay.show_status(StatusView(StatusState.IDLE), replace(ANCHOR, tail=True))
    pump(0.2)
    out["icon_tail"] = list(overlay.status_geometry())
    overlay.show_status(StatusView(StatusState.IDLE), ANCHOR.moved(50, 20))
    pump(0.2)
    out["icon_moved"] = list(overlay.status_geometry())
    overlay.hide_status()
    pump(0.2)
    out["icon_hidden"] = [overlay.status_geometry(), overlay.status_window.get_visible(),
                          overlay._spin is not None]
    holder_clicks()

    def selection_shot(theme: str) -> None:
        overlay.hide_panel()
        overlay.show_panel(SELECTION, 640, 160)
        pump(0.4)
        grab(f"v3-selection-{theme}", [overlay.panel_geometry()], pad=16)

    def icon_shots(theme: str) -> None:
        """Each icon state, the strip a muted icon opens and the hover menu."""
        for name, view in (("idle", StatusView(StatusState.IDLE)),
                           ("working", StatusView(StatusState.WORKING, spin=True)),
                           ("ready", StatusView(StatusState.READY, 6)),
                           ("tone", StatusView(StatusState.READY)),
                           ("muted", StatusView(StatusState.MUTED, message=NO_AI_LINE))):
            overlay.show_status(view, ANCHOR)
            pump(0.35)
            icon_shot(f"v3-icon-{name}-{theme}")
        overlay.show_strip(StripView(note=NO_AI_LINE, on_demand=True), ANCHOR)
        overlay.show_status(StatusView(StatusState.MUTED, message=NO_AI_LINE), ANCHOR)
        pump(0.4)
        shot(f"v3-muted-strip-{theme}")
        overlay.hide_strip()
        for name, view in (("toolbar", StatusView(StatusState.READY, 6)),
                           ("toolbar-muted", StatusView(StatusState.MUTED, message=PAUSED_LINE))):
            bar_for["view"] = view
            overlay.show_status(view, ANCHOR)
            pump(0.3)
            ix, iy, iw, ih = overlay.status_geometry()
            xtest.fake_input(d, X.MotionNotify, x=ix + iw // 2, y=iy + ih // 2)
            xtest.fake_input(d, X.ButtonPress, 3)
            xtest.fake_input(d, X.ButtonRelease, 3)
            d.sync()
            pump(0.7)
            grab(f"v3-{name}-{theme}", [field(), overlay.toolbar_geometry()])
            xtest.fake_input(d, X.MotionNotify, x=5, y=5)
            d.sync()
            pump(0.7)
        bar_for["view"] = StatusView(StatusState.IDLE)
        # "Friendlier" clicked: its rewrite in the strip, the icon breathing, then ready
        overlay.show_status(StatusView(StatusState.WORKING, spin=True), ANCHOR)
        overlay.show_strip(replace(TONE_STRIP, segments=TONE_STRIP.segments[:4], n_fixes=0,
                                   loading="Rewriting…"), ANCHOR)
        pump(0.4)
        shot(f"v3-tone-loading-{theme}")
        overlay.show_status(StatusView(StatusState.IDLE), ANCHOR)
        overlay.show_strip(TONE_STRIP, ANCHOR)
        pump(0.4)
        out[f"tone_strip_text_{theme}"] = overlay.strip_text()
        shot(f"v3-tone-strip-{theme}")
        overlay.hide_strip()
        overlay.hide_status()
        pump(0.2)

    def icon_shot(name: str) -> None:
        f = ANCHOR.field
        grab(name, [(f.x + f.w - 240, f.y, 240, 50)], pad=12)

    def extra_shots(theme: str) -> None:
        """The ghost text after the field's last word, and the shortcuts window."""
        ghost = GtkGhost()
        page.label.set_text(GHOST_TYPED)
        pump(0.2)
        x, y, h = page.text_end()
        f = ANCHOR.field
        ghost.show(" and we can demo it Monday", x, y, round(h * 0.81), f.x + f.w - 12 - x)
        pump(0.4)
        grab(f"v3-ghost-{theme}", [field(), ghost.geometry()])
        ghost.hide()
        page.label.set_text(TYPED)
        win = ShortcutsWindow()
        win.show(glance_shortcuts(GeneralCfg().hotkeys))
        pump(0.6)
        grab(f"v3-shortcuts-{theme}", [[*win.window.get_position(), *win.window.get_size()]],
             pad=10)
        win.window.destroy()
        pump(0.2)

    icon_shots("light")

    # ---- the collapsed strip
    overlay.show_strip(STRIP, ANCHOR)
    pump(0.4)
    out["strip"] = real(overlay.strip_window)
    out["strip_reported"] = list(overlay.strip_geometry())
    out["strip_text"] = overlay.strip_text()
    out["strip_buttons"] = [button_text(b) for b in buttons(overlay.strip_window)]
    out["strip_override_redirect"] = override_redirect(overlay.strip_window)
    out["focusable"] = [[w.get_accept_focus(), w.get_focus_on_map(), w.is_active()]
                        for w in (overlay.status_window, overlay.strip_window,
                                  overlay.panel_window)]
    # theme: light unless told otherwise
    overlay.show_status(StatusView(StatusState.READY, STRIP.n_fixes), ANCHOR)
    pump(0.3)
    shot("v3-strip-light")
    overlay.hide_status()
    sx, sy, _sw, sh = overlay.strip_geometry()
    holder_clicks()
    click(sx + 60, sy + sh // 2)  # on the sentence: not a button, so it goes through
    out["click_through_sentence"] = holder_clicks()
    press(overlay.strip_window, "Alt+Enter")
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
    out["strip_open"] = overlay.strip_geometry()  # the panel shows the text itself
    out["panel"] = real(overlay.panel_window)
    out["panel_reported"] = list(overlay.panel_geometry())
    out["panel_override_redirect"] = override_redirect(overlay.panel_window)
    out["focus_with_panel"] = focused()
    out["drawer_buttons"] = [button_text(b) for b in buttons(overlay.panel_window)]
    shot("v3-panel-light")
    overlay.show_panel(SELECTED, 0, 0, anchor=ANCHOR)
    pump(0.4)
    out["selected_buttons"] = [button_text(b) for b in buttons(overlay.panel_window)]
    shot("v3-panel-selected-light")
    overlay.show_panel(WORKING, 0, 0, anchor=ANCHOR)
    pump(0.4)
    shot("v3-panel-working-light")
    overlay.show_panel(DRAWER, 0, 0, anchor=ANCHOR)
    pump(0.4)
    for text in ("Shorter", "Rewrite as…", "Fix All", "hey → Hey", "its → it's"):
        press(overlay.panel_window, text)
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
    overlay.show_status(StatusView(StatusState.READY, STRIP.n_fixes), ANCHOR)
    pump(0.3)
    shot("v3-strip-dark")
    overlay.hide_status()
    overlay.show_strip(StripView(STRIP.segments, STRIP.n_fixes, expanded=True), ANCHOR)
    overlay.show_panel(DRAWER, 0, 0, anchor=ANCHOR)
    pump(0.5)
    out["dark_panel"] = overlay.panel_geometry()
    out["dark_strip"] = overlay.strip_geometry()
    shot("v3-panel-dark")
    overlay.show_panel(SELECTED, 0, 0, anchor=ANCHOR)
    pump(0.4)
    shot("v3-panel-selected-dark")
    overlay.show_panel(WORKING, 0, 0, anchor=ANCHOR)
    pump(0.4)
    shot("v3-panel-working-dark")
    overlay.hide_panel()
    overlay.hide_strip()
    icon_shots("dark")
    extra_shots("dark")
    selection_shot("dark")
    overlay.hide_panel()
    overlay._theme.force(False)
    page.dark(False)
    pump(0.3)
    extra_shots("light")

    # ---- select-anything's panel, at a point
    overlay.hide_panel()
    overlay.show_panel(SELECTION, 5000, 5000)
    pump(0.4)
    out["selection"] = real(overlay.panel_window)
    out["selection_buttons"] = [button_text(b) for b in buttons(overlay.panel_window)]
    selection_shot("light")
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
    out["hidden"] = [overlay.status_geometry(), overlay.strip_geometry(),
                     overlay.panel_geometry(), overlay.strip_window.get_visible(),
                     overlay.panel_window.get_visible()]
    out["events"] = events
    out["consts"] = {"gap": GAP, "shadow": list(overlay._shadow)}
    sys.stdout.write(json.dumps(out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
