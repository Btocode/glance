"""Drives Glance Shot's X11 renderer on whatever $DISPLAY is; prints one JSON line.

    shot_probe.py [--shots DIR]

Run by tests/test_shot_xvfb.py against a private Xvfb server: it drags and clicks with XTEST,
so never point it at a display somebody is using. The "screen" it captures is a stand-in app
it draws itself. With --shots it saves shot-picker-<theme>.png, shot-bar-<theme>.png and
shot-notice-<theme>.png (light and dark) in DIR.
"""
import argparse
import json
import sys
import time
from pathlib import Path

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk
from Xlib import X, display
from Xlib.ext import xtest

from glance.protocol import Rect
from glance.shot import bar_model as bm
from glance.shot.image import png_size
from glance.ui.shot_gtk import GtkShot

ERROR = ("Traceback (most recent call last):\n"
         '  File "deploy.py", line 42, in <module>\n'
         "    client.push(release)\n"
         "ConnectionRefusedError: [Errno 111] Connection refused")
EXTRACTED = ("Build failed\nConnectionRefusedError: [Errno 111] Connection refused\n"
             "deploy.py, line 42: client.push(release)")


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


class Keys:
    """The hotkey port: records what the picker grabs."""

    def __init__(self):
        self.grabs, self.log = {}, []

    def grab(self, name, chord, cb):
        self.grabs[name] = (chord, cb)
        self.log.append(("grab", name, chord))
        return True

    def ungrab(self, name):
        self.grabs.pop(name, None)
        self.log.append(("ungrab", name))


class Scenery:
    """A stand-in desktop: a page and an app window showing an error, under everything."""

    def __init__(self):
        self.css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), self.css, 900)
        self.page = Gtk.Window(type=Gtk.WindowType.POPUP)
        self.page.set_name("page")
        self.page.move(0, 0)
        self.page.set_size_request(1280, 800)
        self.app = Gtk.Window(type=Gtk.WindowType.POPUP)
        self.app.set_name("app")
        self.app.move(160, 120)
        self.app.set_size_request(760, 300)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_start(28)
        box.set_margin_top(24)
        title = Gtk.Label(label="Build failed", xalign=0)
        title.set_name("title")
        body = Gtk.Label(label=ERROR, xalign=0)
        body.set_name("body")
        box.pack_start(title, False, False, 0)
        box.pack_start(body, False, False, 0)
        self.app.add(box)
        self.dark(False)
        for w in (self.page, self.app):
            w.show_all()

    def dark(self, on: bool):
        page, box, ink, line = (("#0f141a", "#1b222c", "#e6edf3", "#2a3340") if on else
                                ("#e9edf1", "#ffffff", "#17191c", "#d8dee4"))
        self.css.load_from_data(f"""
            #page {{ background-color: {page}; }}
            #app {{ background-color: {box}; border: 1px solid {line}; }}
            #title {{ color: {ink}; font-size: 20px; font-weight: 600; }}
            #body {{ color: {ink}; font-family: monospace; font-size: 14px; }}""".encode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots")
    args = ap.parse_args()
    out: dict = {}
    d = display.Display()
    scenery = Scenery()
    pump(0.4)
    # The app being used: it holds the input focus; the picker and the bar must not take it.
    holder = d.screen().root.create_window(0, 0, 1, 1, 0, d.screen().root_depth)
    holder.map()
    d.sync()
    pump(0.1)
    holder.set_input_focus(X.RevertToParent, X.CurrentTime)
    d.sync()

    def focused() -> bool:
        return d.get_input_focus().focus.id == holder.id

    keys = Keys()
    acts: list = []
    shot = GtkShot(keys, acts.append)
    results: list = []

    def save(name: str, rect: tuple[int, int, int, int] | None = None) -> None:
        if not args.shots:
            return
        x, y, w, h = rect or (0, 0, 1280, 800)
        x0, y0 = max(0, x), max(0, y)
        w, h = min(1280 - x0, w), min(800 - y0, h)
        pix = Gdk.pixbuf_get_from_window(Gdk.get_default_root_window(), x0, y0, w, h)
        pix.savev(str(Path(args.shots) / f"{name}.png"), "png", [], [])

    def override_redirect(win: Gtk.Window) -> bool:
        xid = win.get_window().get_xid()
        return bool(d.create_resource_object("window", xid).get_attributes().override_redirect)

    def xdrag(x0, y0, x1, y1):
        xtest.fake_input(d, X.MotionNotify, x=x0, y=y0)
        d.sync()
        pump(0.05)
        xtest.fake_input(d, X.ButtonPress, 1)
        d.sync()
        pump(0.05)
        for i in range(1, 6):
            xtest.fake_input(d, X.MotionNotify, x=x0 + (x1 - x0) * i // 5,
                             y=y0 + (y1 - y0) * i // 5)
            d.sync()
            pump(0.03)

    def xrelease():
        xtest.fake_input(d, X.ButtonRelease, 1)
        d.sync()
        pump(0.2)

    def click_button(key: str) -> bool:
        for b in buttons(shot.bar_window):
            if b.get_name() == f"shot:{key}":
                wx, wy = shot.bar_window.get_position()
                bx, by = b.translate_coordinates(shot.bar_window, 0, 0)
                a = b.get_allocation()
                xtest.fake_input(d, X.MotionNotify, x=wx + bx + a.width // 2,
                                 y=wy + by + a.height // 2)
                xtest.fake_input(d, X.ButtonPress, 1)
                xtest.fake_input(d, X.ButtonRelease, 1)
                d.sync()
                pump(0.3)
                return True
        return False

    region = Rect(160, 120, 760, 300)

    def bar_views():
        row = bm.buttons(vision=True, ai=True, images="ask", ocr=True, private=False)
        text = bm.ShotBarView(
            "760 × 300", bm.buttons(vision=True, ai=True, images="ask", ocr=True,
                                    private=False),
            result=bm.ShotResult("Text", EXTRACTED, "ready",
                                 bm.text_actions(ai=True, private=False),
                                 note="Read on this computer"))
        notice = bm.ShotBarView("760 × 300", row, notice=bm.ShotNotice(
            bm.notice_text("Anthropic (api.anthropic.com)")))
        return text, notice

    for theme in ("light", "dark"):
        shot._theme.force(theme == "dark")
        scenery.dark(theme == "dark")
        pump(0.3)
        shot.pick(results.append)
        pump(0.3)
        win = shot.picker_window
        out[f"picker_{theme}"] = {
            "override_redirect": override_redirect(win),
            "focusable": [win.get_accept_focus(), win.get_focus_on_map(), win.is_active()],
            "grabbed": sorted(n for n, (_c, _cb) in keys.grabs.items()),
            "size": list(win.get_size()),
        }
        xdrag(150, 108, 930, 432)
        out[f"label_{theme}"] = shot.picker.label() if shot.picker else None
        out[f"focus_while_picking_{theme}"] = focused()
        pump(0.2)
        save(f"shot-picker-{theme}")
        xrelease()
        res = results[-1] if results else None
        out[f"result_{theme}"] = None if res is None else {
            "rect": [res.shot.rect.x, res.shot.rect.y, res.shot.rect.w, res.shot.rect.h],
            "png": list(png_size(res.shot.png)), "bytes": len(res.shot.png)}
        out[f"picker_closed_{theme}"] = shot.picker_window is None and not keys.grabs
        text, notice = bar_views()
        shot.show_bar(text, region)
        pump(0.4)
        geo = shot.bar_geometry()
        out[f"bar_{theme}"] = geo
        out[f"bar_override_redirect_{theme}"] = override_redirect(shot.bar_window)
        out[f"bar_focus_{theme}"] = [shot.bar_window.get_accept_focus(),
                                     shot.bar_window.is_active()]
        x, y, w, h = geo
        pad = 24
        top = min(region.y, y)
        save(f"shot-bar-{theme}", (min(region.x, x) - pad, top - pad,
                                   max(region.x + region.w, x + w) - min(region.x, x) + 2 * pad,
                                   max(region.y + region.h, y + h) - top + 2 * pad))
        shot.show_bar(notice, region)
        pump(0.4)
        x, y, w, h = shot.bar_geometry()
        save(f"shot-notice-{theme}", (x - pad, y - pad, w + 2 * pad, h + 2 * pad))
        acts.clear()
        out[f"clicked_{theme}"] = [click_button("notice_send"), click_button("copy")]
        out[f"acts_{theme}"] = list(acts)
        out[f"focus_after_bar_{theme}"] = focused()
        shot.hide_bar()

    # Esc through the grab, and a cancelled pick reports None
    shot.pick(results.append)
    pump(0.2)
    n = len(results)
    keys.grabs["shot_pick_escape"][1]()
    pump(0.1)
    out["escape_cancels"] = len(results) == n + 1 and results[-1] is None
    # Enter: the whole screen
    shot.pick(results.append)
    pump(0.2)
    keys.grabs["shot_pick_return"][1]()
    pump(0.1)
    full = results[-1]
    out["enter_full"] = [full.shot.rect.w, full.shot.rect.h] if full else None
    # the clipboard gets the image
    ok = shot.copy_image(full.shot.png)
    pump(0.2)
    img = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).wait_for_image()
    out["clipboard"] = [ok, None if img is None else [img.get_width(), img.get_height()]]
    shot.close()
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
