"""Selection actions on whatever $DISPLAY is (a private Xvfb only: it clicks with XTEST):
the mark, the action bar and the result panel, and the X11 selection watcher. Prints one
JSON line.

    selection_probe.py [--shots DIR]

With --shots it saves sel-<piece>-<theme>.png in DIR: the mark beside a selection, the
action bar, the bar with "⋯" open, an answer (Explain), a translation that may replace the
selection, an answer still being written, and the text a Glance Shot read.
"""
import argparse
import json
import sys
import time
from pathlib import Path

import cairo
import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango
from Xlib import X, Xatom, display
from Xlib.ext import xtest

from glance.ui import selection_bar as sb
from glance.ui.overlay_gtk import GtkOverlay, button_text
from glance.ui.panel_model import PanelView, ResultView
from glance.ui.selection_watch_xlib import XlibSelectionWatch
from glance.ui.theme import install

PARA = ("Our Q3 numbers came in ahead of plan: revenue grew 18% year over year while "
        "support tickets fell for the third quarter in a row. The team proposes moving the "
        "pricing change to January so the migration can finish first.")
SELECTED = "revenue grew 18% year over year while support tickets fell"
EXPLAIN = ("Sales income was 18% higher than in the same quarter last year, and at the same "
           "time customers needed less help: fewer support requests were opened.")
SPANISH = "los ingresos crecieron un 18 % interanual mientras bajaban los tickets de soporte"
SHOT_TEXT = ("Error: connection refused (port 5432)\nIs the server runnning on host \"db\" and "
             "accepting TCP/IP connections?")
PAGE = (40, 60, 760, 220)


def pump(seconds: float) -> None:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.005)


def buttons(widget):
    if isinstance(widget, Gtk.Button):
        return [widget]
    if isinstance(widget, Gtk.Container):
        return [b for c in widget.get_children() for b in buttons(c)]
    return []


class Page:
    """A reading page with part of a paragraph selected (drawn highlighted). No clicks."""

    def __init__(self) -> None:
        self.css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), self.css, 900)
        self.win = Gtk.Window(type=Gtk.WindowType.POPUP)
        x, y, w, h = PAGE
        self.win.move(x, y)
        self.win.set_size_request(w, h)
        self.win.set_name("page")
        self.label = Gtk.Label(xalign=0, wrap=True, max_width_chars=70)
        self.label.set_line_wrap_mode(Pango.WrapMode.WORD)
        self.label.set_margin_start(28)
        self.label.set_margin_end(28)
        self.label.set_margin_top(26)
        self.label.set_valign(Gtk.Align.START)
        self.win.add(self.label)
        self.dark(False)
        self.win.show_all()
        self.win.get_window().input_shape_combine_region(cairo.Region(), 0, 0)

    def dark(self, on: bool) -> None:
        page, ink, sel = (("#1b222c", "#e6edf3", "#264f78") if on else
                          ("#ffffff", "#17191c", "#b3d4fc"))
        a = PARA.index(SELECTED)
        esc = GLib.markup_escape_text
        self.label.set_markup(esc(PARA[:a]) + f'<span background="{sel}">'
                              + esc(SELECTED) + "</span>" + esc(PARA[a + len(SELECTED):]))
        self.css.load_from_data(f"""
            #page {{ background-color: {page}; border: 1px solid alpha(#888888, 0.3); }}
            #page label {{ color: {ink}; font-size: 16px; }}""".encode())

    def selection_end(self) -> tuple[int, int]:
        """Where the pointer let go: the end of the highlighted words."""
        layout = self.label.get_layout()
        ox, oy = self.label.get_layout_offsets()
        end = len((PARA[:PARA.index(SELECTED)] + SELECTED).encode())
        pos = layout.index_to_pos(end)
        k = 1024
        return (PAGE[0] + ox + pos.x // k, PAGE[1] + oy + (pos.y + pos.height // 2) // k)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots")
    args = ap.parse_args()
    out: dict = {}
    events: list = []
    overlay = GtkOverlay(lambda: None, lambda action, a: events.append([action, a]))
    theme = install()
    d = display.Display()
    root = d.screen().root
    holder = root.create_window(0, 0, 1280, 800, 0, d.screen().root_depth,
                                background_pixel=d.screen().white_pixel)
    holder.map()
    d.sync()
    pump(0.2)
    holder.set_input_focus(X.RevertToParent, X.CurrentTime)
    d.sync()
    page = Page()
    pump(0.3)

    def focused() -> bool:
        return d.get_input_focus().focus.id == holder.id

    def click(x: int, y: int) -> None:
        xtest.fake_input(d, X.MotionNotify, x=x, y=y)
        xtest.fake_input(d, X.ButtonPress, 1)
        xtest.fake_input(d, X.ButtonRelease, 1)
        d.sync()
        pump(0.3)

    def grab(name: str, rects: list, pad: int = 20) -> None:
        if not args.shots:
            return
        rects = [r for r in rects if r]
        x0 = max(0, min(r[0] for r in rects) - pad)
        y0 = max(0, min(r[1] for r in rects) - pad)
        x1 = min(1280, max(r[0] + r[2] for r in rects) + pad)
        y1 = min(800, max(r[1] + r[3] for r in rects) + pad)
        pix = Gdk.pixbuf_get_from_window(Gdk.get_default_root_window(), x0, y0, x1 - x0,
                                         y1 - y0)
        pix.savev(str(Path(args.shots) / f"{name}.png"), "png", [], [])

    end = page.selection_end()
    out["end"] = end
    mark_xy = sb.place_mark(*end, overlay.monitors())

    # ---------------------------------------------------------------- the mark and the bar
    def pieces(theme_name: str) -> None:
        dark = theme_name == "dark"
        theme.force(dark)
        page.dark(dark)
        overlay.hide_selmark()
        overlay.show_selmark(sb.mark_view(), *mark_xy)
        xtest.fake_input(d, X.MotionNotify, x=5, y=5)
        d.sync()
        pump(0.3)
        grab(f"sel-mark-{theme_name}", [PAGE])
        bar = sb.bar_view("English", hint="Super+Alt+A")
        overlay.show_selmark(bar, *mark_xy)
        pump(0.3)
        grab(f"sel-bar-{theme_name}", [PAGE, overlay.selmark_geometry()])
        more = sb.bar_view("English", (("friendlier", "Friendlier"), ("shorter", "Shorter"),
                                       ("professional", "Professional")), True)
        overlay.show_selmark(more, *mark_xy)
        pump(0.3)
        grab(f"sel-bar-more-{theme_name}", [PAGE, overlay.selmark_geometry()])
        overlay.hide_selmark()
        src = sb.excerpt(SELECTED)
        for name, res in (
                ("explain", ResultView("Explain", src, EXPLAIN, "ready")),
                ("translate", ResultView("Translate to Spanish", src, SPANISH, "ready",
                                         can_replace=True)),
                ("working", ResultView("Summary", src, None, "loading")),
                # Glance Shot's Extract text, handed to this panel
                ("shot-text", ResultView("Extracted text", "", SHOT_TEXT, "ready", actions=(
                    ("fix", "Fix grammar"), ("translate", "Translate to English"),
                    ("explain", "Explain")), origin="shot_text"))):
            overlay.hide_panel()
            overlay.show_panel(PanelView(mode="result", result=res), end[0], end[1] + 20)
            pump(0.4)
            grab(f"sel-result-{name}-{theme_name}", [PAGE, overlay.panel_geometry()], pad=16)
        overlay.hide_panel()

    pieces("light")
    pieces("dark")
    theme.force(False)
    page.dark(False)

    # ---------------------------------------------------------------- behaviour
    overlay.show_selmark(sb.mark_view(), *mark_xy)
    pump(0.3)
    sel = overlay.selmark_window()
    gx, gy, gw, gh = overlay.selmark_geometry()
    out["mark_geometry"] = [gx, gy, gw, gh]
    out["mark_override_redirect"] = bool(
        d.create_resource_object("window", sel.get_window().get_xid())
        .get_attributes().override_redirect)
    out["mark_hit"] = [overlay.selmark_hit(gx + 3, gy + 3), overlay.selmark_hit(5, 5)]
    events.clear()
    click(gx + gw // 2, gy + gh // 2)
    out["mark_click"] = [e[0] for e in events if e[0] != "sel_hover"]
    out["mark_hover"] = [e[1].get("inside") for e in events if e[0] == "sel_hover"]
    overlay.show_selmark(sb.bar_view("English"), *mark_xy)
    pump(0.3)
    target = next(b for b in buttons(sel) if button_text(b) == "Explain")
    bx, by = target.translate_coordinates(sel, 0, 0)
    a = target.get_allocation()
    wx, wy = sel.get_position()
    events.clear()
    click(wx + bx + a.width // 2, wy + by + a.height // 2)
    out["bar_click"] = [e for e in events if e[0] == "sel_action"]
    out["focus_kept"] = focused()
    overlay.hide_selmark()
    pump(0.2)
    out["hidden"] = overlay.selmark_geometry() is None

    # ---------------------------------------------------------------- the X11 watcher
    seen: list = []
    watch = XlibSelectionWatch(lambda ms, fn: _Later(ms, fn), time.monotonic)
    watch.start(lambda x, y: seen.append(["settled", x, y]),
                lambda: seen.append(["cleared"]),
                lambda kind, x, y: seen.append(["input", kind]))
    out["watch_running"] = watch.running
    owner_d = display.Display()
    owner = owner_d.screen().root.create_window(0, 0, 1, 1, 0, X.CopyFromParent)
    owner_d.flush()

    def take_primary() -> None:
        owner.set_selection_owner(Xatom.PRIMARY, X.CurrentTime)
        owner_d.flush()

    # a mouse selection: the button down, the owner taken, the button up, then quiet
    xtest.fake_input(d, X.MotionNotify, x=500, y=200)
    xtest.fake_input(d, X.ButtonPress, 1)
    d.sync()
    pump(0.15)
    take_primary()
    pump(0.3)
    xtest.fake_input(d, X.MotionNotify, x=520, y=210)
    xtest.fake_input(d, X.ButtonRelease, 1)
    d.sync()
    pump(0.15)
    out["settled_early"] = list(seen)
    pump(0.6)
    out["mouse"] = list(seen)
    seen.clear()
    # a keyboard selection (no button): cleared, never settled
    pump(0.8)
    take_primary()
    pump(0.8)
    out["keyboard"] = list(seen)
    seen.clear()
    # keys reach it only while armed
    xtest.fake_input(d, X.KeyPress, 38)
    xtest.fake_input(d, X.KeyRelease, 38)
    d.sync()
    pump(0.3)
    out["key_unarmed"] = list(seen)
    watch.arm(True)
    pump(0.1)
    xtest.fake_input(d, X.KeyPress, 38)
    xtest.fake_input(d, X.KeyRelease, 38)
    xtest.fake_input(d, X.ButtonPress, 4)
    xtest.fake_input(d, X.ButtonRelease, 4)
    d.sync()
    pump(0.3)
    out["armed"] = list(seen)
    watch.arm(False)
    watch.stop()
    print(json.dumps(out))
    return 0


class _Later:
    """clock.call_later over GLib, for the watcher."""

    def __init__(self, ms: int, fn) -> None:
        self._id = GLib.timeout_add(ms, self._fire)
        self._fn = fn

    def _fire(self) -> bool:
        self._id = 0
        self._fn()
        return False

    def cancel(self) -> None:
        if self._id:
            GLib.source_remove(self._id)
            self._id = 0


if __name__ == "__main__":
    sys.exit(main())
