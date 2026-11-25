"""Drives the ghost overlay, the Autocomplete engine and real key grabs on whatever $DISPLAY
is, and prints what happened as one JSON line.

    ghost_probe.py

Run by tests/desktop/test_ghost.py against a private Xvfb server (no window manager). It
sends keys and clicks with XTEST, so never point it at a display somebody is using.

The "app" is a GTK TextView in a window titled glance-test-app that holds the X input focus.
What the daemon's Controller would do is done here by a few lines: a snapshot of the
TextView after every change, and `request_apply` inserting into its buffer.
"""
import json
import sys
import time

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk
from Xlib import XK, X, display
from Xlib.ext import shape, xtest
from Xlib.protocol import rq, structs

from glance.audience.profiles import BUILTIN
from glance.audience.resolver import DEFAULT_RULES, AudienceResolver
from glance.clock import GLibClock
from glance.config import Config
from glance.features.autocomplete import Autocomplete
from glance.protocol import ApplyResult, FieldSnapshot, Rect
from glance.ui.ghost_gtk import GtkGhost
from glance.ui.hotkeys_xlib import XlibHotkeys

SHAPE_INPUT = 2


class GetInputRectangles(rq.ReplyRequest):
    """XShapeGetRectangles for the input region: python-xlib only knows bounding and clip."""

    _request = rq.Struct(
        rq.Card8("opcode"), rq.Opcode(8), rq.RequestLength(), rq.Window("window"),
        rq.Card8("region"), rq.Pad(3))
    _reply = rq.Struct(
        rq.ReplyCode(), rq.Card8("ordering"), rq.Card16("sequence_number"), rq.ReplyLength(),
        rq.LengthOf("rectangles", 4), rq.Pad(20), rq.List("rectangles", structs.Rectangle))


COMPLETION = " tomorrow morning."
TITLE = "glance-test-app"


def pump(seconds: float, until=None) -> bool:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if until is not None and until():
            return True
        time.sleep(0.005)
    return bool(until and until())


class NoOverrides:
    def get(self, key):
        return None


class Supervisor:
    """The part of BackendSupervisor Autocomplete calls: always answers COMPLETION."""

    def complete(self, prefix, ctx, speculative=False):
        return [COMPLETION]

    def live_autocomplete(self):
        return False

    def auto_allowed(self):
        return True

    def cancel_all(self):
        pass

    def cancel_speculative(self):
        pass


class Controller:
    """What Autocomplete asks of the Controller, on top of the TextView."""

    generation = 0

    def __init__(self, app):
        self.app = app
        self.extra_status = []
        self.listeners = []
        self.n = 0

    def add_listener(self, listener):
        self.listeners.append(listener)

    def request_apply(self, edit, method, base_sha):
        self.n += 1
        apply_id = f"a{self.n}"
        buf = self.app.buf
        buf.insert(buf.get_iter_at_offset(edit.start), edit.replacement)
        GLib.idle_add(lambda: self.listeners[0].on_apply_result(
            ApplyResult(apply_id, "verified", None)) or False)
        return apply_id


class App:
    def __init__(self, d):
        self.d = d
        self.win = Gtk.Window(title=TITLE)
        self.win.set_default_size(480, 320)
        self.win.move(100, 100)
        self.view = Gtk.TextView(wrap_mode=Gtk.WrapMode.NONE, accepts_tab=True)
        self.buf = self.view.get_buffer()
        self.win.add(self.view)
        self.win.show_all()
        pump(0.4)
        self.xid = self.win.get_window().get_xid()
        d.create_resource_object("window", self.xid).set_input_focus(X.RevertToParent,
                                                                    X.CurrentTime)
        d.sync()
        self.view.grab_focus()
        self.seq = 0
        self.on_snapshot = lambda s: None
        self.buf.connect("changed", self._changed)
        self.buf.connect("notify::cursor-position", self._changed)

    def _changed(self, *_):
        GLib.idle_add(self.push)

    def text(self) -> str:
        return self.buf.get_text(self.buf.get_start_iter(), self.buf.get_end_iter(), True)

    def caret(self) -> int:
        return self.buf.get_iter_at_mark(self.buf.get_insert()).get_offset()

    def root_rect(self, offset: int) -> Rect:
        loc = self.view.get_iter_location(self.buf.get_iter_at_offset(offset))
        wx, wy = self.view.buffer_to_window_coords(Gtk.TextWindowType.TEXT, loc.x, loc.y)
        ox, oy = self.view.get_window(Gtk.TextWindowType.TEXT).get_origin()[1:]
        return Rect(ox + wx, oy + wy, max(1, loc.width), loc.height)

    def push(self) -> bool:
        self.seq += 1
        text = self.text()
        ox, oy = self.view.get_window(Gtk.TextWindowType.TEXT).get_origin()[1:]
        alloc = self.view.get_allocation()
        self.on_snapshot(FieldSnapshot(
            app=TITLE, wm_class=TITLE, title=TITLE, url=None, role="text", text=text,
            caret=self.caret(), caret_rect=self.root_rect(self.caret()),
            prev_char_rect=None, field_rect=Rect(ox, oy, alloc.width, alloc.height),
            window_rect=Rect(100, 100, 480, 320), field_id="f1", capture_confidence=1.0,
            seq=self.seq, pid=1, gated=None))
        return False

    def set_text(self, text: str, caret: int | None = None) -> None:
        self.buf.set_text(text)
        self.buf.place_cursor(self.buf.get_iter_at_offset(len(text) if caret is None else caret))
        self.d.create_resource_object("window", self.xid).set_input_focus(
            X.RevertToParent, X.CurrentTime)
        self.view.grab_focus()
        pump(0.15)
        self.push()
        pump(0.1)


def main() -> int:
    d = display.Display()
    kd = display.Display()  # XTEST on its own connection, as another program would

    def keysym(name):
        code = kd.keysym_to_keycode(XK.string_to_keysym(name))
        assert code, name
        return code

    def chord(*names):
        codes = [keysym(n) for n in names]
        for c in codes:
            xtest.fake_input(kd, X.KeyPress, c)
        for c in reversed(codes):
            xtest.fake_input(kd, X.KeyRelease, c)
        kd.sync()

    app = App(d)
    ghost = GtkGhost()
    ctrl = Controller(app)
    hotkeys = XlibHotkeys(lambda cb: GLib.idle_add(lambda: cb() or False))
    bg = lambda work, done: GLib.idle_add(lambda: done(work()) or False)
    ac = Autocomplete(ctrl, Supervisor(), AudienceResolver(BUILTIN, DEFAULT_RULES, NoOverrides()),
                      ghost, hotkeys, GLibClock(), bg, Config)
    ac.attach()
    app.on_snapshot = ac.on_snapshot
    assert hotkeys.grab("complete", "Super+Alt+Return", ac.trigger)

    def status() -> dict:
        out = {}
        for provider in ctrl.extra_status:
            out.update(provider())
        return out

    def visible() -> bool:
        return bool(status().get("autocomplete_visible"))

    def ac_grabs() -> list[str]:
        return sorted(n for n in hotkeys.grabbed() if n.startswith("ac_"))

    def suggest(text: str = "I will send it") -> None:
        app.set_text(text)
        chord("Super_L", "Alt_L", "Return")
        assert pump(3.0, visible), "no suggestion appeared"

    out: dict = {"start_grabs": sorted(hotkeys.grabbed())}

    # 1. no suggestion: Tab is the application's
    app.set_text("")
    for i in range(200):
        chord("Tab")
        if i % 20 == 19:
            pump(0.05)
    pump(0.5)
    out["tabs_inserted"] = app.text().count("\t")
    out["tabs_text_is_only_tabs"] = set(app.text()) <= {"\t"}
    out["idle_grabs"] = ac_grabs()

    # 2. Tab accepts all
    suggest()
    out["grabs_while_visible"] = ac_grabs()
    out["ghost_visible_while_visible"] = ghost.window.get_visible()
    chord("Tab")
    pump(1.0, lambda: "tomorrow" in app.text())
    pump(0.3)
    out["after_tab"] = app.text()
    out["visible_after_tab"] = visible()
    out["grabs_after_tab"] = ac_grabs()
    out["ghost_visible_after_tab"] = ghost.window.get_visible()

    # 3. the next Tab is normal again
    chord("Tab")
    pump(0.4)
    out["tab_after_accept"] = app.text()[len("I will send it tomorrow morning."):]

    # 4. Ctrl+Right takes one word, the rest stays on screen
    suggest()
    chord("Control_L", "Right")
    pump(2.0, lambda: app.text().endswith("tomorrow") and visible())
    out["after_ctrl_right"] = app.text()
    out["visible_after_ctrl_right"] = visible()
    out["ghost_text_after_ctrl_right"] = ghost.shown_text()
    out["grabs_after_ctrl_right"] = ac_grabs()
    chord("Control_L", "Right")
    pump(1.0, lambda: app.text().endswith("morning."))
    pump(0.3)
    out["after_second_ctrl_right"] = app.text()
    out["visible_after_second_ctrl_right"] = visible()

    # 5. typing dismisses at once, and gives Tab back
    suggest()
    t0 = time.monotonic()
    chord("x")
    gone = pump(1.0, lambda: not visible())
    out["dismissed_in_s"] = time.monotonic() - t0 if gone else None
    out["grabs_after_typing"] = ac_grabs()
    out["ghost_visible_after_typing"] = ghost.window.get_visible()
    chord("Tab")
    pump(0.4)
    out["text_after_typing_then_tab"] = app.text()

    # 6. click-through, no focus
    app.set_text("line one\nline two is here\nline three", caret=0)
    target = app.root_rect(len("line one\n") + 8)
    out["caret_before_click"] = app.caret()
    ghost.show("GHOSTGHOSTGHOST", target.x - 2, target.y, 16, 300)
    pump(0.4)
    gx, gy, gw, gh = ghost.geometry()
    out["click_inside_ghost"] = (gx <= target.x + 3 < gx + gw and gy <= target.y + 5 < gy + gh)
    xtest.fake_input(kd, X.MotionNotify, x=target.x + 3, y=target.y + target.h // 2)
    xtest.fake_input(kd, X.ButtonPress, 1)
    xtest.fake_input(kd, X.ButtonRelease, 1)
    kd.sync()
    pump(0.5)
    out["caret_after_click"] = app.caret()
    out["caret_wanted"] = len("line one\n") + 8
    out["focus_is_app"] = d.get_input_focus().focus.id == app.xid
    gxid = ghost.window.get_window().get_xid()
    gwin = d.create_resource_object("window", gxid)
    out["ghost_override_redirect"] = bool(gwin.get_attributes().override_redirect)
    reply = GetInputRectangles(display=d.display, opcode=d.display.get_extension_major(shape.extname),
                               window=gxid, region=SHAPE_INPUT)
    out["ghost_input_rects"] = len(reply.rectangles)
    out["ghost_flags"] = [ghost.window.get_accept_focus(), ghost.window.get_focus_on_map(),
                          ghost.window.is_active()]
    out["ghost_font_px"] = ghost.font_px()
    ghost.hide()

    # 7. clamped to the monitor
    ghost.show("a long completion that cannot fit at the right edge of the screen", 1270, 790,
               16, 400)
    pump(0.3)
    out["clamped"] = list(ghost.geometry())
    ghost.hide()
    ghost.show("x" * 300, 100, 100, 16, 150)
    pump(0.3)
    out["narrow"] = list(ghost.geometry())
    ghost.hide()

    hotkeys.close()
    sys.stdout.write(json.dumps(out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
