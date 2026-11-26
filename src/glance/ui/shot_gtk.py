"""Glance Shot on X11: the region picker, the shot bar and the image clipboard (needs a live X
session).

The picker freezes the screen first (XGetImage of the root window, before anything of Glance's
is drawn), then covers every monitor with one override-redirect window that shows that frame
dimmed: the user drags a rectangle out of it. Being override-redirect the window manager
never manages it, so it never takes the keyboard focus from the app; Enter, Space and Esc are
grabbed (XGrabKey, the hotkey port) only while it is up. The capture is the frozen frame cut
to the region, encoded as PNG in memory.

The bar is a popup like the assistant panel (accept_focus off); the clipboard image is served
by GTK from glanced (the CLIPBOARD owner) until another program takes the clipboard.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import cairo
import gi

gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gtk", "3.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango, PangoCairo

from glance.log import log_event
from glance.protocol import Rect
from glance.shot import bar_model as bm
from glance.shot import geometry as geo
from glance.shot.controller import PickResult
from glance.shot.image import Shot, bgrx_to_rgb, crop, encode_png, scaled_rect
from glance.shot.picker import HINT, Picker
from glance.ui import look
from glance.ui.overlay_gtk import (
    NO_SHADOW,
    SHADOW,
    _classes,
    _clear,
    _kbd,
    _label,
    _mark,
    _pointer,
    _popup,
    _raise,
    _rounded,
)
from glance.ui.ports import WindowInfo
from glance.ui.theme import FONT, MONO, install, rgb, rgba

DIM = 0.45  # the frozen screen outside the region
BAR_W = 560
_RESULT_MAX_H = 240
PICK_KEYS = (("shot_pick_return", "Return"), ("shot_pick_space", "space"),
             ("shot_pick_escape", "Escape"))


# ---------------------------------------------------------------------- X: frame and windows


class XFrame:
    """The root window's pixels (BGRX, as cairo's RGB24 and X's ZPixmap both lay them out)
    and the top-level windows, read once when the picker opens. Own X connection, main loop
    only."""

    def __init__(self, display_name: str | None = None) -> None:
        from Xlib import X, display

        self._X = X
        self._d = display.Display(display_name)
        self._root = self._d.screen().root
        atom = self._d.intern_atom
        self._stacking = atom("_NET_CLIENT_LIST_STACKING")
        self._extents = atom("_NET_FRAME_EXTENTS")
        self._name = atom("_NET_WM_NAME")
        self._pid = atom("_NET_WM_PID")
        self._state = atom("_NET_WM_STATE")
        self._hidden = atom("_NET_WM_STATE_HIDDEN")

    def grab(self) -> tuple[bytes, int, int] | None:
        """(BGRX pixels, width, height) of the whole root, or None (an unusual visual)."""
        geom = self._root.get_geometry()
        w, h = int(geom.width), int(geom.height)
        if self._d.screen().root_depth not in (24, 32):
            log_event("shot_unsupported_depth")
            return None
        try:
            img = self._root.get_image(0, 0, w, h, self._X.ZPixmap, 0xFFFFFFFF)
        except Exception as e:  # noqa: BLE001 - Xlib errors: no capture, no crash
            log_event("shot_grab_failed", kind=type(e).__name__)
            return None
        data = img.data if isinstance(img.data, bytes) else bytes(img.data)
        if len(data) < w * h * 4:
            return None
        return data, w, h

    def windows(self) -> list[WindowInfo]:
        """Viewable top-level windows with their frames, topmost first."""
        from Xlib import error

        out: list[WindowInfo] = []
        try:
            prop = self._root.get_full_property(self._stacking, self._X.AnyPropertyType)
        except error.XError:
            return out
        ids = [int(i) for i in prop.value] if prop is not None else []
        for xid in reversed(ids):
            try:
                w = self._d.create_resource_object("window", xid)
                if w.get_attributes().map_state != self._X.IsViewable:
                    continue
                state = w.get_full_property(self._state, self._X.AnyPropertyType)
                if state is not None and self._hidden in list(state.value):
                    continue
                g = w.get_geometry()
                pos = self._root.translate_coords(w, 0, 0)
                x, y, ww, hh = int(pos.x), int(pos.y), int(g.width), int(g.height)
                ext = w.get_full_property(self._extents, self._X.AnyPropertyType)
                if ext is not None and len(ext.value) == 4:
                    left, right, top, bottom = (int(v) for v in ext.value)
                    x, y, ww, hh = x - left, y - top, ww + left + right, hh + top + bottom
                cls = w.get_wm_class()
                name = w.get_full_property(self._name, self._X.AnyPropertyType)
                raw = name.value if name is not None else (w.get_wm_name() or "")
                title = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
                pid = w.get_full_property(self._pid, self._X.AnyPropertyType)
                out.append(WindowInfo(
                    wm_class=(cls[1] or cls[0]) if cls else "", title=title,
                    pid=int(pid.value[0]) if pid is not None and len(pid.value) else 0,
                    xid=xid, rect=Rect(x, y, ww, hh)))
            except error.XError:
                continue
        return out

    def close(self) -> None:
        self._d.close()


# ---------------------------------------------------------------------- the renderer


class GtkShot:
    """ShotUiPort on X11. `on_action(key)` gets the bar's clicks (copy, save, extract,
    explain, ask, translate, close, text_*, notice_*)."""

    def __init__(self, hotkeys: Any, on_action: Callable[[str], None],
                 frame: Callable[[], Any] = XFrame) -> None:
        self._hotkeys = hotkeys
        self._act = on_action
        self._frame_factory = frame
        self._theme = install()
        self._theme.on_change(self._restyle)
        screen = Gdk.Screen.get_default()
        self._composited = bool(screen is not None and screen.is_composited())
        self._shadow = SHADOW if self._composited else NO_SHADOW
        # picker
        self.picker: Picker | None = None
        self._on_done: Callable[[PickResult | None], None] | None = None
        self._pixels: tuple[bytes, int, int] | None = None
        self._surface: cairo.ImageSurface | None = None
        self._windows: list[WindowInfo] = []
        self._scale = 1
        self._pick_win: Gtk.Window | None = None
        # bar
        self._bar = _popup(Gdk.WindowTypeHint.POPUP_MENU)
        self._bar.connect("draw", _clear)
        self._bar.connect("size-allocate", lambda *_a: self._place_bar())
        self._bar.connect("map", lambda *_a: self._bar_shape())
        self._bar_view: bm.ShotBarView | None = None
        self._region: Rect | None = None
        self._bar_xy: tuple[int, int] | None = None

    # ------------------------------------------------------------------ the picker

    def pick(self, on_done: Callable[[PickResult | None], None]) -> None:
        self.cancel_pick()
        self._on_done = on_done
        try:
            frame = self._frame_factory()
            try:
                pixels = frame.grab()
                windows = frame.windows() if pixels is not None else []
            finally:
                frame.close()
        except Exception as e:  # noqa: BLE001 - no display access: say nothing was taken
            log_event("shot_frame_failed", kind=type(e).__name__)
            pixels, windows = None, []
        if pixels is None:
            self._finish(None)
            return
        self._pixels = pixels
        data, w, h = pixels
        self._surface = cairo.ImageSurface.create_for_data(
            bytearray(data[:w * h * 4]), cairo.FORMAT_RGB24, w, h, w * 4)
        self._windows = windows
        mons = self._monitors()
        self.picker = Picker(mons, [x.rect for x in windows if x.rect is not None])
        win = Gtk.Window(type=Gtk.WindowType.POPUP)
        win.set_accept_focus(False)
        win.set_focus_on_map(False)
        win.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
        win.set_keep_above(True)
        win.set_app_paintable(True)
        win.get_style_context().add_class("glance-root")
        bounds = self.picker.screen
        win.move(bounds.x, bounds.y)
        win.set_default_size(bounds.w, bounds.h)
        win.set_size_request(bounds.w, bounds.h)
        win.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                       | Gdk.EventMask.POINTER_MOTION_MASK)
        win.connect("draw", self._draw_picker)
        win.connect("button-press-event", self._pressed)
        win.connect("button-release-event", self._released)
        win.connect("motion-notify-event", self._moved)
        win.connect("realize", lambda w: w.get_window().set_cursor(
            Gdk.Cursor.new_from_name(w.get_display(), "crosshair")))
        self._pick_win = win
        win.show_all()
        _raise(win)
        self._scale = max(1, win.get_scale_factor())
        for name, key in PICK_KEYS:
            try:
                self._hotkeys.grab(name, key, lambda k=key: self.key(k))
            except ValueError:
                pass
        log_event("shot_picker_open", monitors=len(mons), windows=len(windows))

    def _monitors(self) -> list[Rect]:
        display = Gdk.Display.get_default()
        out = []
        for i in range(display.get_n_monitors()):
            g = display.get_monitor(i).get_geometry()
            out.append(Rect(g.x, g.y, g.width, g.height))
        return out

    def key(self, name: str) -> None:
        """Enter, Space or Esc while the picker is up (from the grabs; tests call it too)."""
        if self.picker is None:
            return
        if self._pick_win is not None and name.casefold() == "space":
            ptr = self._pointer()
            if ptr is not None:
                self.picker.motion(*ptr)
        out = self.picker.key(name)
        if out is not None:
            self._end(out.region)
        else:
            self._redraw()

    def _pointer(self) -> tuple[int, int] | None:
        display = Gdk.Display.get_default()
        seat = display.get_default_seat() if display is not None else None
        if seat is None:
            return None
        _screen, x, y = seat.get_pointer().get_position()
        return int(x), int(y)

    def _pressed(self, _w: Gtk.Widget, event: Gdk.EventButton) -> bool:
        if self.picker is None:
            return True
        if event.button == 3:  # right click: cancel, as Esc
            self._end(None)
            return True
        if event.button == 1:
            self.picker.press(int(event.x_root), int(event.y_root))
            self._redraw()
        return True

    def _moved(self, _w: Gtk.Widget, event: Gdk.EventMotion) -> bool:
        if self.picker is not None:
            self.picker.motion(int(event.x_root), int(event.y_root))
            self._redraw()
        return True

    def _released(self, _w: Gtk.Widget, event: Gdk.EventButton) -> bool:
        if self.picker is None or event.button != 1:
            return True
        out = self.picker.release(int(event.x_root), int(event.y_root))
        if out is not None:
            self._end(out.region)
        else:
            self._redraw()
        return True

    def _redraw(self) -> None:
        if self._pick_win is not None:
            self._pick_win.queue_draw()

    def drag(self, x0: int, y0: int, x1: int, y1: int, release: bool = True) -> None:
        """A drag as the pointer would make it (the screenshot probe uses it)."""
        if self.picker is None:
            return
        self.picker.press(x0, y0)
        self.picker.motion(x1, y1)
        if release:
            out = self.picker.release(x1, y1)
            if out is not None:
                self._end(out.region)
                return
        self._redraw()

    def _draw_picker(self, _w: Gtk.Widget, cr: Any) -> bool:
        p = self.picker
        if p is None or self._surface is None:
            return True
        t = self._theme.tokens
        ox, oy = p.screen.x, p.screen.y
        cr.save()
        cr.scale(1 / self._scale, 1 / self._scale)
        cr.set_source_surface(self._surface, -ox * self._scale, -oy * self._scale)
        cr.get_source().set_filter(cairo.FILTER_FAST)
        cr.paint()
        cr.restore()
        sel = p.selection
        hover = p.hover_window()
        lit = sel or hover
        # dim everything but the region (or the window Space would take)
        cr.set_source_rgba(0.03, 0.05, 0.07, DIM)
        cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        cr.rectangle(0, 0, p.screen.w, p.screen.h)
        if lit is not None:
            cr.rectangle(lit.x - ox, lit.y - oy, lit.w, lit.h)
        cr.fill()
        cr.set_fill_rule(cairo.FILL_RULE_WINDING)
        if lit is not None:
            cr.set_source_rgb(*rgb(t, "accent_fill"))
            cr.set_line_width(2.0 if sel is not None else 1.5)
            if sel is None:
                cr.set_dash([6.0, 4.0])
            cr.rectangle(lit.x - ox + 1, lit.y - oy + 1, max(0, lit.w - 2), max(0, lit.h - 2))
            cr.stroke()
            cr.set_dash([])
        label = p.label()
        if label is not None:
            self._pill(cr, label, p.label_at, ox, oy, t, mono=True)
        else:
            mon = geo.monitor_at(*p.pointer, p.monitors) or p.screen
            self._pill(cr, HINT, lambda size: (mon.x + (mon.w - size[0]) // 2, mon.y + 28),
                       ox, oy, t)
        return True

    def _pill(self, cr: Any, text: str, where: Callable[[tuple[int, int]], tuple[int, int]],
              ox: int, oy: int, t: dict[str, str], mono: bool = False) -> None:
        layout = Pango.Layout(self._pick_win.get_pango_context())
        desc = Pango.FontDescription.from_string(
            f"{(MONO if mono else FONT).split(',')[0].strip(chr(39))} {12 if mono else 13}")
        layout.set_font_description(desc)
        layout.set_text(text, -1)
        tw, th = layout.get_pixel_size()
        w, h = tw + 20, th + 10
        x, y = where((w, h))
        x, y = x - ox, y - oy
        r = h / 2
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -1.5708, 1.5708)
        cr.arc(x + r, y + r, r, 1.5708, 4.7124)
        cr.close_path()
        cr.set_source_rgba(*rgba(t["surface"])[:3], 0.94)
        cr.fill_preserve()
        cr.set_source_rgba(*rgba(t["hairline"]))
        cr.set_line_width(1)
        cr.stroke()
        cr.set_source_rgb(*rgb(t, "text"))
        cr.move_to(x + 10, y + 5)
        PangoCairo.show_layout(cr, layout)

    def _end(self, region: Rect | None) -> None:
        pixels, windows = self._pixels, self._windows
        origin = self.picker.screen if self.picker is not None else None
        self._close_picker()
        if region is None or pixels is None or origin is None:
            self._finish(None)
            return
        data, w, h = pixels
        box = scaled_rect(region, origin, self._scale)
        box = geo.intersect(box, Rect(0, 0, w, h))
        if box is None:
            self._finish(None)
            return
        rgb_px = bgrx_to_rgb(crop(data, w, 4, box), box.w, box.h)
        png = encode_png(rgb_px, box.w, box.h)
        self._finish(PickResult(Shot(png, box.w, box.h, region), tuple(windows)))

    def _finish(self, result: PickResult | None) -> None:
        done, self._on_done = self._on_done, None
        if done is not None:
            done(result)

    def _close_picker(self) -> None:
        for name, _key in PICK_KEYS:
            try:
                self._hotkeys.ungrab(name)
            except Exception as e:  # noqa: BLE001 - closing must never fail
                log_event("shot_ungrab_failed", kind=type(e).__name__)
        if self._pick_win is not None:
            self._pick_win.destroy()
            self._pick_win = None
        self.picker = None
        self._pixels = None  # the frozen frame is dropped with the picker
        self._surface = None
        self._windows = []

    def cancel_pick(self) -> None:
        if self.picker is not None or self._pick_win is not None:
            self._close_picker()
            self._finish(None)

    @property
    def picker_window(self) -> Gtk.Window | None:
        return self._pick_win

    # ------------------------------------------------------------------ the bar

    def show_bar(self, view: bm.ShotBarView, region: Rect) -> None:
        changed = view != self._bar_view or region != self._region
        self._region = region
        if changed:
            self._bar_view = view
            self._rebuild(view)
        if not self._bar.get_visible():
            self._bar.show_all()
            _raise(self._bar)
        self._place_bar()

    def hide_bar(self) -> None:
        self._bar.hide()
        self._bar_view = None
        self._region = None
        self._bar_xy = None

    @property
    def bar_window(self) -> Gtk.Window:
        return self._bar

    def bar_geometry(self) -> tuple[int, int, int, int] | None:
        if not self._bar.get_visible():
            return None
        return (*self._bar.get_position(), *self._bar.get_size())

    def _place_bar(self) -> None:
        if self._region is None or not self._bar.get_visible():
            return
        w, h = self._bar.get_size()
        sh = self._shadow
        x, y = geo.bar_position(self._region, (w - sh[0] - sh[2], h - sh[1] - sh[3]),
                                self._monitors())
        xy = (x - sh[0], y - sh[1])
        if xy != self._bar_xy:
            self._bar_xy = xy
            self._bar.move(*xy)
        self._bar_shape()

    def _bar_shape(self) -> None:
        gdk_window = self._bar.get_window()
        if gdk_window is None:
            return
        w, h = self._bar.get_size()
        sh = self._shadow
        card = (sh[0], sh[1], max(1, w - sh[0] - sh[2]), max(1, h - sh[1] - sh[3]))
        gdk_window.input_shape_combine_region(cairo.Region(cairo.RectangleInt(*card)), 0, 0)
        if not self._composited:
            gdk_window.shape_combine_region(_rounded(*card, 12), 0, 0)

    def _button(self, b: bm.ShotButton) -> Gtk.Button:
        button = _pointer(Gtk.Button(label=b.label, can_focus=False, focus_on_click=False))
        _classes(button, "glance-primary" if b.primary and not b.muted else "glance-secondary")
        if b.muted:
            _classes(button, "glance-muted")
        if b.tip:
            button.set_tooltip_text(b.tip)
        button.set_name(f"shot:{b.key}")
        button.connect("clicked", lambda _b: self._act(b.key))
        return button

    def _header(self, view: bm.ShotBarView) -> Gtk.Widget:
        header = Gtk.Box(spacing=10)
        header.pack_start(_mark(20), False, False, 0)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        titles.set_valign(Gtk.Align.CENTER)
        titles.pack_start(_label(bm.TITLE, "glance-title", wrap=False), False, False, 0)
        titles.pack_start(_label(view.size, "glance-meta", wrap=False), False, False, 0)
        header.pack_start(titles, True, True, 0)
        close = _classes(_pointer(Gtk.Button(can_focus=False, focus_on_click=False)),
                         "glance-close")
        inner = Gtk.Box(spacing=6)
        inner.pack_start(_kbd(look.ESC_KEY), False, False, 0)
        inner.pack_start(_label(look.CLOSE, wrap=False), False, False, 0)
        close.add(inner)
        close.set_name("shot:close")
        close.set_valign(Gtk.Align.CENTER)
        close.connect("clicked", lambda _b: self._act("close"))
        header.pack_end(close, False, False, 0)
        return header

    def _rebuild(self, view: bm.ShotBarView) -> None:
        old = self._bar.get_child()
        if old is not None:
            self._bar.remove(old)
            old.destroy()
        card = _classes(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12),
                        "glance-drawer", "glance-shot")
        card.set_size_request(BAR_W, -1)
        card.pack_start(self._header(view), False, False, 0)
        row = Gtk.Box(spacing=6)
        for b in view.buttons:
            if b.key != "close":  # the header's "Esc Close"
                row.pack_start(self._button(b), False, False, 0)
        card.pack_start(row, False, False, 0)
        if view.notice is not None:
            box = _classes(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8),
                           "glance-banner")
            box.pack_start(_label(view.notice.text, chars=64), False, False, 0)
            actions = Gtk.Box(spacing=6)
            for b in view.notice.buttons:
                actions.pack_start(self._button(b), False, False, 0)
            box.pack_start(actions, False, False, 0)
            card.pack_start(box, False, False, 0)
        if view.banner:
            card.pack_start(_classes(_label(view.banner, chars=64),
                                     "glance-banner" if view.warn else "glance-done"),
                            False, False, 0)
        if view.result is not None:
            card.pack_start(self._result(view.result), False, False, 0)
        outer = Gtk.Box()
        outer.set_margin_start(self._shadow[0])
        outer.set_margin_top(self._shadow[1])
        outer.set_margin_end(self._shadow[2])
        outer.set_margin_bottom(self._shadow[3])
        outer.pack_start(card, True, True, 0)
        outer.show_all()
        self._bar.add(outer)
        self._bar.resize(1, 1)  # shrink to the new content

    def _result(self, r: bm.ShotResult) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        head = Gtk.Box(spacing=8)
        head.pack_start(_label(r.title, "glance-section", wrap=False), False, False, 0)
        if r.note:
            note = _label(r.note, "glance-meta", wrap=False)
            note.set_ellipsize(Pango.EllipsizeMode.END)
            head.pack_end(note, False, False, 0)
        box.pack_start(head, False, False, 0)
        if r.text is not None:
            text = Gtk.Label(label=r.text, xalign=0, yalign=0, wrap=True, max_width_chars=66,
                             selectable=False)
            text.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            scroll = Gtk.ScrolledWindow()
            scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroll.set_propagate_natural_height(True)
            scroll.set_max_content_height(_RESULT_MAX_H)
            scroll.add(text)
            box.pack_start(_classes(scroll, "glance-result"), False, False, 0)
        elif r.state == "loading":
            box.pack_start(_label("…", "glance-meta"), False, False, 0)
        if r.actions and r.text is not None:
            actions = Gtk.Box(spacing=6)
            for b in r.actions:
                actions.pack_start(self._button(b), False, False, 0)
            box.pack_start(actions, False, False, 0)
        return box

    def _restyle(self) -> None:
        if self._bar_view is not None:
            self._rebuild(self._bar_view)
        self._redraw()

    # ------------------------------------------------------------------ clipboard

    def copy_image(self, png: bytes) -> bool:
        try:
            loader = GdkPixbuf.PixbufLoader.new_with_type("png")
            loader.write(png)
            loader.close()
            pixbuf = loader.get_pixbuf()
            clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            clipboard.set_image(pixbuf)
            clipboard.store()
            return True
        except GLib.Error as e:
            log_event("shot_copy_failed", kind=type(e).__name__)
            return False

    def close(self) -> None:
        self.cancel_pick()
        self.hide_bar()
