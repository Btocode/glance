"""Drives Glance Shot's X11 renderer on whatever $DISPLAY is; prints one JSON line.

    shot_probe.py [--shots DIR]

Run by tests/test_shot_xvfb.py against a private Xvfb server: it drags and clicks with XTEST,
so never point it at a display somebody is using. The "screen" it captures is a stand-in app
it draws itself. With --shots it saves shot-picker-<theme>.png, shot-bar-<theme>.png and
shot-notice-<theme>.png (light and dark) in DIR.
"""
import argparse
import dataclasses
import json
import sys
import time
from pathlib import Path

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk
from Xlib import XK, X, display
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
    shot = GtkShot(keys, lambda key, args=None: acts.append(key if args is None else [key, args]))
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
        def tabs(**kw):
            return bm.buttons(vision=True, ai=True, images="ask", ocr=True, private=False, **kw)

        size, tools = "760 × 300", bm.tools()
        start = bm.ShotBarView(size, tabs(), tools=tools)
        text = bm.ShotBarView(size, tabs(shown="extract"), banner="Text copied to the clipboard",
                              result=bm.ShotResult("Text", EXTRACTED, "ready", bm.text_actions(),
                                                   note="Read on this computer"), tools=tools)
        loading = bm.ShotBarView(size, tabs(busy="explain"), tools=tools,
                                 result=bm.ShotResult("Explanation", None, "loading",
                                                      note="Thinking…"))
        explain = bm.ShotBarView(size, tabs(shown="explain"), tools=tools, result=bm.ShotResult(
            "Explanation", "The deploy script could not reach the release server: nothing "
            "was listening on the address it connected to (connection refused). Check that "
            "the server is running and the host and port in deploy.py are right.", "ready",
            bm.ANSWER_ACTIONS, note="Answered by Anthropic"))
        notice = bm.ShotBarView(size, tabs(shown="explain"), tools=tools, notice=bm.ShotNotice(
            bm.notice_text("Anthropic (api.anthropic.com)")))
        choose = bm.ShotBarView(size, tabs(shown="translate"), tools=tools, result=bm.ShotResult(
            bm.TRANSLATE_TO, None, "choose", choices=bm.languages("English")))
        asking = bm.ShotBarView(size, tabs(shown="ask"), tools=tools, expanded=True, asking=True)
        copied = bm.ShotBarView(size, tabs(), tools=tools, banner="Image copied to the clipboard")
        views = [start, text, loading, explain, notice, choose, asking, copied]
        views = [v if v is asking or v is start or v is copied
                 else dataclasses.replace(v, expanded=True) for v in views]
        return tuple(views)

    def save_scene(name, pad=24):
        x, y, w, h = shot.bar_geometry()
        left, top = min(region.x, x) - pad, min(region.y, y) - pad
        save(name, (left, top, max(region.x + region.w, x + w) + pad - left,
                    max(region.y + region.h, y + h) + pad - top))

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
        start, text, loading, explain, notice, choose, asking, copied = bar_views()
        shot.show_bar(text, region)
        pump(0.4)
        geo = shot.bar_geometry()
        out[f"bar_{theme}"] = geo
        out[f"bar_override_redirect_{theme}"] = override_redirect(shot.bar_window)
        out[f"bar_focus_{theme}"] = [shot.bar_window.get_accept_focus(),
                                     shot.bar_window.is_active()]
        save_scene(f"shot-bar-{theme}")
        tops = []
        for name, view in (("start", start), ("copied", copied), ("text", text),
                           ("loading", loading), ("explain", explain), ("notice", notice),
                           ("choose", choose), ("asking", asking)):
            shot.show_bar(view, region)
            pump(0.4)
            tops.append(shot.bar_geometry()[:2])
            save_scene(f"shot-{name}-{theme}")
        out[f"card_stays_put_{theme}"] = len(set(tops)) == 1
        # Ask in the card: typed there (the keyboard is the card's while it is open)
        acts.clear()
        for ch in ("h", "i", "Return"):
            code = d.keysym_to_keycode(XK.string_to_keysym(ch))
            xtest.fake_input(d, X.KeyPress, code)
            xtest.fake_input(d, X.KeyRelease, code)
        d.sync()
        pump(0.4)
        out[f"ask_typed_{theme}"] = list(acts)
        out[f"focus_after_ask_{theme}"] = focused()
        # Mark: a box dragged on the shot, then Done
        marked = []
        shot.hide_bar()
        shot.annotate(res.shot, marked.append)
        pump(0.4)
        mk = shot.marker
        cx, cy = res.shot.rect.x, res.shot.rect.y
        for key in ("mark_box",):
            mk.act(key)
        xdrag(cx + 40, cy + 40, cx + 300, cy + 140)
        xrelease()
        mk.act("mark_arrow")
        xdrag(cx + 500, cy + 250, cx + 320, cy + 150)
        xrelease()
        pump(0.3)
        mx, my = mk.bar.get_position()
        mw, mh = mk.bar.get_size()
        save(f"shot-mark-{theme}", (min(cx, mx) - 24, min(cy, my) - 24,
                                    max(cx + res.shot.rect.w, mx + mw) - min(cx, mx) + 48,
                                    max(cy + res.shot.rect.h, my + mh) - min(cy, my) + 48))
        out[f"mark_strokes_{theme}"] = [t for t, _p in mk.strokes]
        mk.finish()
        pump(0.2)
        out[f"marked_{theme}"] = bool(marked and marked[0] and marked[0] != res.shot.png)
        shot.show_bar(notice, region)
        pump(0.3)
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
