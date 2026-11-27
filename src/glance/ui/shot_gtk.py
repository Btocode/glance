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
    glyph_path,
)
from glance.ui.ports import WindowInfo
from glance.ui.theme import FONT, MONO, install, rgb, rgba

DIM = 0.45  # the frozen screen outside the region
BAR_W = 460  # the card
_RESULT_MAX_H = 260
_BODY_MIN_H = 132  # the body keeps at least this height: the card barely resizes per step
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
        # the captured region, outlined while its card is up (clicks go through it)
        self._frame = _popup(Gdk.WindowTypeHint.TOOLTIP)
        self._frame.connect("draw", self._draw_frame)
        self._entry: Gtk.Entry | None = None
        self._keys_taken = False
        self._marker: Any = None  # the Mark window while the shot is being marked up
        self._placed_for: Rect | None = None

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
        self._show_frame(region)
        if not self._bar.get_visible():
            self._bar.show_all()
            _raise(self._bar)
        self._place_bar()

    _FRAME_PAD = 3  # the outline sits just outside the region: the shot itself stays clear

    def _show_frame(self, region: Rect) -> None:
        p = self._FRAME_PAD
        x, y, w, h = region.x - p, region.y - p, region.w + 2 * p, region.h + 2 * p
        self._frame.set_size_request(max(1, w), max(1, h))
        self._frame.resize(max(1, w), max(1, h))
        self._frame.move(x, y)
        if not self._frame.get_visible():
            self._frame.show_all()
            self._frame.move(x, y)
        gdk_window = self._frame.get_window()
        if gdk_window is not None:
            gdk_window.input_shape_combine_region(cairo.Region(), 0, 0)
            if not self._composited:  # a ring: the region shows through it
                ring = cairo.Region(cairo.RectangleInt(0, 0, w, h))
                ring.subtract(cairo.RectangleInt(2, 2, max(0, w - 4), max(0, h - 4)))
                gdk_window.shape_combine_region(ring, 0, 0)
        self._frame.queue_draw()

    def _draw_frame(self, widget: Gtk.Widget, cr: Any) -> bool:
        _clear(widget, cr)
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        cr.set_source_rgba(*rgba(self._theme.tokens["accent_fill"]))
        cr.set_line_width(2)
        cr.rectangle(1, 1, w - 2, h - 2)
        cr.stroke()
        return True

    def hide_bar(self) -> None:
        self._release_keys()
        if self._marker is not None:
            self._marker.cancel()
        self._frame.hide()
        self._bar.hide()
        self._bar_view = None
        self._region = None
        self._bar_xy = None
        self._placed_for = None

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
        if self._bar_xy is not None and self._placed_for == self._region:
            # Placed once for this shot: the toolbar stays exactly there while the card
            # grows and shrinks under it (moved up only if it would leave the screen).
            x, y = self._bar_xy
            mons = self._monitors()
            mon = geo.monitor_at(x + sh[0], y + sh[1], mons)
            if mon is not None and y + h > mon.y + mon.h:
                y = max(mon.y, mon.y + mon.h - h)
            xy = (x, y)
        else:
            x, y = geo.bar_position(self._region, (w - sh[0] - sh[2], h - sh[1] - sh[3]),
                                    self._monitors())
            xy = (x - sh[0], y - sh[1])
            self._placed_for = self._region
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

    def _button(self, b: bm.ShotButton, *classes: str) -> Gtk.Button:
        button = _pointer(Gtk.Button(label=b.label, can_focus=False, focus_on_click=False))
        if classes:
            _classes(button, *classes)
        else:
            _classes(button, "glance-primary" if b.primary and not b.muted
                     else "glance-secondary")
        if b.muted:
            _classes(button, "glance-muted")
        if b.selected:
            _classes(button, "glance-selected")
        if b.tip:
            button.set_tooltip_text(b.tip)
        button.set_name(f"shot:{b.key}")
        button.connect("clicked", lambda _b: self._act(b.key))
        return button

    def _glyph(self, name: str, px: int = 14, token: str = "accent") -> Gtk.Widget:
        area = Gtk.DrawingArea()
        area.set_size_request(px, px)
        area.set_valign(Gtk.Align.CENTER)

        def draw(_w: Gtk.Widget, cr: Any) -> bool:
            glyph_path(cr, name, 0.5, 0.5, px - 1)
            cr.set_source_rgb(*rgb(self._theme.tokens, token))
            cr.set_line_width(1.5)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.set_line_join(cairo.LINE_JOIN_ROUND)
            cr.stroke()
            return True

        area.connect("draw", draw)
        return area

    def _item(self, b: bm.ShotButton, label: bool = True) -> Gtk.Button:
        """One toolbar item, as the selection bar draws them: a line glyph and its label."""
        button = _pointer(Gtk.Button(can_focus=False, focus_on_click=False))
        _classes(button, "glance-item", "glance-selbtn", "glance-shot-item")
        if b.selected:
            _classes(button, "glance-selected")
        if b.muted:
            _classes(button, "glance-muted")
        inner = Gtk.Box(spacing=5)
        glyph = bm.GLYPHS.get(b.key)
        if glyph is not None:
            inner.pack_start(self._glyph(glyph, 13, "muted" if b.muted else "accent"),
                             False, False, 0)
        if label:
            inner.pack_start(_label(b.label, wrap=False), False, False, 0)
        button.add(inner)
        button.set_tooltip_text(b.tip or (None if label else b.label))
        button.set_name(f"shot:{b.key}")
        button.connect("clicked", lambda _b: self._act(b.key))
        return button

    def _toolbar(self, view: bm.ShotBarView) -> Gtk.Widget:
        """The slim bar: the mark, the tabs, │ Mark · Copy · Save │ close. It is the card's
        top row too, and never moves while the card grows under it."""
        row = Gtk.Box(spacing=2)
        lead = _mark(16)
        lead.set_margin_start(4)
        lead.set_margin_end(4)
        lead.set_tooltip_text(f"{bm.TITLE} · {view.size}")
        row.pack_start(lead, False, False, 0)
        for b in view.buttons:
            if b.key != "close":
                row.pack_start(self._item(b), False, False, 0)
        row.pack_start(_classes(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL),
                                "glance-shot-sep"), False, False, 4)
        for b in view.tools:
            row.pack_start(self._item(b), False, False, 0)
        row.pack_start(_classes(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL),
                                "glance-shot-sep"), False, False, 4)
        row.pack_start(self._item(bm.ShotButton("close", f"{look.CLOSE} (Esc)"), label=False),
                       False, False, 0)
        return row

    def _rebuild(self, view: bm.ShotBarView) -> None:
        self._release_keys()
        old = self._bar.get_child()
        if old is not None:
            self._bar.remove(old)
            old.destroy()
        self._entry = None
        card = _classes(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10),
                        "glance-menu", "glance-shot")
        card.pack_start(self._toolbar(view), False, False, 0)
        if view.expanded:
            _classes(card, "glance-shot-open")
            card.pack_start(self._body(view), False, False, 0)
            foot = self._foot(view)
            if foot is not None:
                card.pack_start(foot, False, False, 0)
        elif view.banner:
            card.pack_start(self._status(view), False, False, 0)
        outer = Gtk.Box()
        outer.set_margin_start(self._shadow[0])
        outer.set_margin_top(self._shadow[1])
        outer.set_margin_end(self._shadow[2])
        outer.set_margin_bottom(self._shadow[3])
        outer.pack_start(card, True, True, 0)
        outer.show_all()
        self._bar.add(outer)
        self._bar.resize(1, 1)  # shrink to the new content
        if view.asking and self._entry is not None:
            GLib.idle_add(self._take_keys)

    def _status(self, view: bm.ShotBarView) -> Gtk.Widget:
        status = _classes(_label(view.banner or "", "glance-status", wrap=False),
                          "glance-status")
        if view.warn:
            _classes(status, "glance-warn")
        status.set_ellipsize(Pango.EllipsizeMode.END)
        status.set_xalign(0)
        status.set_margin_start(6)
        status.set_tooltip_text(view.banner)
        return status

    def _body(self, view: bm.ShotBarView) -> Gtk.Widget:
        body = _classes(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8),
                        "glance-shot-body")
        body.set_name("shot:body")
        if view.notice is not None:
            _classes(body, "glance-banner")
            body.pack_start(_label(view.notice.text, chars=52), False, False, 0)
        elif view.asking:
            body.pack_start(_label(bm.ASK_PROMPT, "glance-section", wrap=False),
                            False, False, 0)
            entry = Gtk.Entry(max_length=500)
            entry.set_placeholder_text(bm.ASK_PLACEHOLDER)
            entry.set_name("shot:ask")
            entry.connect("activate", lambda e: self._submit(e.get_text()))
            entry.connect("key-press-event", self._entry_key)
            body.pack_start(entry, False, False, 0)
            hint = Gtk.Box(spacing=6)
            hint.pack_start(_kbd(look.RETURN_KEY), False, False, 0)
            hint.pack_start(_label("ask", "glance-meta", wrap=False), False, False, 0)
            hint.pack_start(_kbd(look.ESC_KEY), False, False, 6)
            hint.pack_start(_label("back", "glance-meta", wrap=False), False, False, 0)
            body.pack_start(hint, False, False, 0)
            self._entry = entry
        elif view.result is not None:
            self._result(body, view.result)
        return body

    def _foot(self, view: bm.ShotBarView) -> Gtk.Widget | None:
        actions: tuple[bm.ShotButton, ...] = ()
        if view.notice is not None:
            actions = view.notice.buttons
        elif view.result is not None and view.result.text is not None and not view.asking:
            actions = view.result.actions
        if not actions and not view.banner:
            return None
        foot = Gtk.Box(spacing=6)
        for b in actions:
            foot.pack_start(self._button(b), False, False, 0)
        if view.banner:
            status = self._status(view)
            status.set_xalign(1)
            foot.pack_end(status, True, True, 0)
        return foot

    def _result(self, box: Gtk.Box, r: bm.ShotResult) -> None:
        head = Gtk.Box(spacing=8)
        head.pack_start(_label(r.title, "glance-section", wrap=False), False, False, 0)
        if r.state == "failed":
            box.pack_start(head, False, False, 0)
            why = _classes(_label(r.note or "That did not work. Try again", chars=52),
                           "glance-status", "glance-warn")
            box.pack_start(why, False, False, 0)
            return
        if r.note:
            note = _label(r.note, "glance-meta", wrap=False)
            note.set_ellipsize(Pango.EllipsizeMode.END)
            head.pack_end(note, False, False, 0)
        box.pack_start(head, False, False, 0)
        if r.state == "choose":
            flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=False,
                               column_spacing=6, row_spacing=6, max_children_per_line=8)
            for c in r.choices:
                chip = self._button(c, "glance-chip")
                flow.add(chip)
            box.pack_start(flow, False, False, 0)
            return
        if r.text is not None:
            text = Gtk.Label(label=r.text, xalign=0, yalign=0, wrap=True, max_width_chars=60,
                             selectable=False)
            text.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            if r.title == "Text":
                _classes(text, "glance-mono")  # what was read, line for line
            scroll = Gtk.ScrolledWindow()
            scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroll.set_propagate_natural_height(True)
            scroll.set_max_content_height(_RESULT_MAX_H)
            scroll.add(text)
            box.pack_start(scroll, False, False, 0)
        elif r.state == "loading":
            for frac in (1.0, 0.86, 0.6):  # skeleton lines, the answer's shape
                bar = _classes(Gtk.Box(), "glance-skeleton")
                bar.set_size_request(int(360 * frac), 10)
                bar.set_halign(Gtk.Align.START)
                box.pack_start(bar, False, False, 2)

    # ------------------------------------------------------------------ Mark

    def annotate(self, shot: Shot, done: Callable[[bytes | None], None]) -> None:
        """Mark up the shot (`Marker`); the card steps aside meanwhile and comes back."""
        if self._marker is not None:
            self._marker.cancel()
        self._release_keys()
        self._bar.hide()
        self._frame.hide()
        self._marker = Marker(self, shot, done)

    @property
    def marker(self) -> Any:
        return self._marker

    # ------------------------------------------------------------------ Ask, in the card

    inline_ask = True  # the controller types Ask's question here, not in a window of its own

    def _take_keys(self) -> bool:
        """The question's entry gets the keyboard while it is open (a grab, as a menu
        takes one); the app keeps its focus and gets the keyboard back on Enter or Esc."""
        entry, gdk_window = self._entry, self._bar.get_window()
        if entry is None or gdk_window is None or self._keys_taken:
            return False
        seat = Gdk.Display.get_default().get_default_seat()
        status = seat.grab(gdk_window, Gdk.SeatCapabilities.KEYBOARD, True, None, None,
                           None, None)
        self._keys_taken = status == Gdk.GrabStatus.SUCCESS
        if not self._keys_taken:
            log_event("shot_ask_grab_failed", status=int(status))
        self._bar.set_focus(entry)
        entry.grab_focus()
        return False

    def _release_keys(self) -> None:
        if self._keys_taken:
            self._keys_taken = False
            Gdk.Display.get_default().get_default_seat().ungrab()

    def _submit(self, text: str) -> None:
        self._release_keys()
        self._act("ask_submit", {"text": text})

    def _entry_key(self, _entry: Gtk.Entry, event: Gdk.EventKey) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            self._release_keys()
            self._act("ask_cancel", {})
            return True
        return False

    def ask_point(self) -> tuple[int, int] | None:
        """Where a one-line entry of its own goes ("Other…" language): under the toolbar."""
        if self._bar_xy is None or not self._bar.get_visible():
            return None
        x, y = self._bar_xy
        return x + self._shadow[0] + 8, y + self._shadow[1] + 52

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


# ---------------------------------------------------------------------- Mark (X11)

MARK_TOOLS = (("pen", "Pen", "pen"), ("box", "Box", "box"), ("arrow", "Arrow", "arrow"),
              ("highlight", "Highlight", "lines"))
MARK_INK = (0.937, 0.267, 0.267, 1.0)        # red: pen, box, arrow
MARK_HIGHLIGHT = (0.98, 0.80, 0.08, 0.38)    # a highlighter's yellow, see-through
MARK_WIDTH, HIGHLIGHT_WIDTH = 3.0, 16.0


def draw_marks(cr: Any, strokes: list[tuple[str, list[tuple[float, float]]]],
               scale: float = 1.0) -> None:
    """The marks, in logical coordinates of the region times `scale` (the PNG's pixels)."""
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    for tool, pts in strokes:
        if not pts:
            continue
        p = [(x * scale, y * scale) for x, y in pts]
        if tool == "highlight":
            cr.set_source_rgba(*MARK_HIGHLIGHT)
            cr.set_line_width(HIGHLIGHT_WIDTH * scale)
            cr.set_line_cap(cairo.LINE_CAP_BUTT)
        else:
            cr.set_source_rgba(*MARK_INK)
            cr.set_line_width(MARK_WIDTH * scale)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
        (x0, y0), (x1, y1) = p[0], p[-1]
        if tool == "box":
            cr.rectangle(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))
            cr.stroke()
        elif tool == "arrow":
            import math

            cr.move_to(x0, y0)
            cr.line_to(x1, y1)
            cr.stroke()
            angle = math.atan2(y1 - y0, x1 - x0)
            head = 14 * scale
            for side in (-0.45, 0.45):
                cr.move_to(x1, y1)
                cr.line_to(x1 - head * math.cos(angle + side), y1 - head * math.sin(angle + side))
            cr.stroke()
        else:
            cr.move_to(*p[0])
            for q in p[1:]:
                cr.line_to(*q)
            if len(p) == 1:
                cr.line_to(p[0][0] + 0.1, p[0][1])
            cr.stroke()


def marked_png(shot: Shot, strokes: list[tuple[str, list[tuple[float, float]]]]) -> bytes:
    """The shot's PNG with the marks drawn on it, at its own pixel size."""
    import io

    surface = cairo.ImageSurface.create_from_png(io.BytesIO(shot.png))
    cr = cairo.Context(surface)
    scale = surface.get_width() / max(1, shot.rect.w)
    draw_marks(cr, strokes, scale)
    out = io.BytesIO()
    surface.write_to_png(out)
    return out.getvalue()


class Marker:
    """Draw on a shot: the frozen region in a window of its own, right where it was taken
    (override-redirect: the app keeps its focus), and a small bar of tools above it. Pen,
    Box, Arrow, Highlight; Undo (Ctrl+Z); Done (Enter) gives the marked-up PNG, Cancel (Esc)
    None. The keyboard is Glance's only while this is up."""

    def __init__(self, owner: GtkShot, shot: Shot, done: Callable[[bytes | None], None]) -> None:
        import io

        self._owner, self._shot, self._done = owner, shot, done
        self.tool = "pen"
        self.strokes: list[tuple[str, list[tuple[float, float]]]] = []
        self._drawing = False
        self._finished = False
        r = shot.rect
        self._image = cairo.ImageSurface.create_from_png(io.BytesIO(shot.png))
        self.canvas = _popup(Gdk.WindowTypeHint.TOOLTIP)
        self.canvas.set_size_request(r.w, r.h)
        self.canvas.resize(r.w, r.h)
        self.canvas.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                               | Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.KEY_PRESS_MASK)
        self.canvas.connect("draw", self._draw)
        self.canvas.connect("button-press-event", self._press)
        self.canvas.connect("motion-notify-event", self._motion)
        self.canvas.connect("button-release-event", self._release)
        self.canvas.connect("key-press-event", self._key)
        self.canvas.move(r.x, r.y)
        self.canvas.show_all()
        self.canvas.move(r.x, r.y)
        gdk_window = self.canvas.get_window()
        if gdk_window is not None:
            gdk_window.set_cursor(Gdk.Cursor.new_from_name(Gdk.Display.get_default(),
                                                           "crosshair"))
        self.bar = _popup(Gdk.WindowTypeHint.POPUP_MENU)
        self.bar.connect("draw", _clear)
        self._build_bar()
        self._place_bar()
        _raise(self.canvas)
        _raise(self.bar)
        self._grabbed = False
        GLib.idle_add(self._grab)

    # -- the bar of tools
    def _build_bar(self) -> None:
        old = self.bar.get_child()
        if old is not None:
            self.bar.remove(old)
            old.destroy()
        row = _classes(Gtk.Box(spacing=2), "glance-menu", "glance-shot")
        lead = _mark(16)
        lead.set_margin_start(4)
        lead.set_margin_end(4)
        row.pack_start(lead, False, False, 0)
        for key, label, _glyph in MARK_TOOLS:
            b = bm.ShotButton(f"mark_{key}", label, selected=key == self.tool)
            row.pack_start(self._button(b, "pen" if key == "pen" else _glyph), False, False, 0)
        row.pack_start(_classes(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL),
                                "glance-shot-sep"), False, False, 4)
        row.pack_start(self._button(bm.ShotButton("mark_undo", "Undo")), False, False, 0)
        row.pack_start(self._button(bm.ShotButton("mark_cancel", "Cancel")), False, False, 0)
        done = _pointer(Gtk.Button(label="Done", can_focus=False, focus_on_click=False))
        _classes(done, "glance-primary")
        done.set_name("shot:mark_done")
        done.connect("clicked", lambda _b: self.finish())
        row.pack_start(done, False, False, 4)
        row.show_all()
        self.bar.add(row)
        self.bar.resize(1, 1)

    def _button(self, b: bm.ShotButton, glyph: str | None = None) -> Gtk.Button:
        button = _pointer(Gtk.Button(can_focus=False, focus_on_click=False))
        _classes(button, "glance-item", "glance-selbtn", "glance-shot-item")
        if b.selected:
            _classes(button, "glance-selected")
        inner = Gtk.Box(spacing=5)
        if glyph in look.GLYPHS:
            inner.pack_start(self._owner._glyph(glyph, 13), False, False, 0)
        inner.pack_start(_label(b.label, wrap=False), False, False, 0)
        button.add(inner)
        button.set_name(f"shot:{b.key}")
        button.connect("clicked", lambda _b: self.act(b.key))
        return button

    def _place_bar(self) -> None:
        r = self._shot.rect
        w, h = self.bar.get_preferred_size()[1].width, self.bar.get_preferred_size()[1].height
        mons = self._owner._monitors()
        mon = geo.monitor_at(r.x + r.w // 2, r.y + r.h // 2, mons) or Rect(0, 0, 10 ** 5, 10 ** 5)
        y = r.y - h - 8 if r.y - h - 8 >= mon.y else r.y + r.h + 8
        if y + h > mon.y + mon.h:
            y = r.y + 8  # no room around it: inside the region's top
        x = max(mon.x, min(r.x, mon.x + mon.w - w))
        self.bar.move(x, y)
        self.bar.show_all()
        self.bar.move(x, y)
        gdk_window = self.bar.get_window()
        if gdk_window is not None and not self._owner._composited:
            gdk_window.shape_combine_region(_rounded(0, 0, w, h, 10), 0, 0)

    def act(self, key: str) -> None:
        if key.startswith("mark_") and key[5:] in {t[0] for t in MARK_TOOLS}:
            self.tool = key[5:]
            self._build_bar()
        elif key == "mark_undo":
            if self.strokes:
                self.strokes.pop()
                self.canvas.queue_draw()
        elif key == "mark_cancel":
            self.cancel()

    # -- drawing
    def _draw(self, widget: Gtk.Widget, cr: Any) -> bool:
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        cr.save()
        cr.scale(w / max(1, self._image.get_width()), h / max(1, self._image.get_height()))
        cr.set_source_surface(self._image, 0, 0)
        cr.paint()
        cr.restore()
        draw_marks(cr, self.strokes)
        cr.set_source_rgba(*rgba(self._owner._theme.tokens["accent_fill"]))
        cr.set_line_width(2)
        cr.rectangle(1, 1, w - 2, h - 2)
        cr.stroke()
        return True

    def _press(self, _w: Gtk.Widget, ev: Gdk.EventButton) -> bool:
        if ev.button == 1:
            self._drawing = True
            self.strokes.append((self.tool, [(ev.x, ev.y)]))
            self.canvas.queue_draw()
        return True

    def _motion(self, _w: Gtk.Widget, ev: Gdk.EventMotion) -> bool:
        if self._drawing and self.strokes:
            tool, pts = self.strokes[-1]
            if tool in ("box", "arrow"):
                pts[1:] = [(ev.x, ev.y)]
            else:
                pts.append((ev.x, ev.y))
            self.canvas.queue_draw()
        return True

    def _release(self, w: Gtk.Widget, ev: Gdk.EventButton) -> bool:
        if ev.button == 1 and self._drawing:
            self._motion(w, ev)  # type: ignore[arg-type]
            self._drawing = False
        return True

    def _key(self, _w: Gtk.Widget, ev: Gdk.EventKey) -> bool:
        if ev.keyval == Gdk.KEY_Escape:
            self.cancel()
        elif ev.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.finish()
        elif ev.keyval in (Gdk.KEY_z, Gdk.KEY_Z) and ev.state & Gdk.ModifierType.CONTROL_MASK:
            self.act("mark_undo")
        return True

    def _grab(self) -> bool:
        gdk_window = self.canvas.get_window()
        if gdk_window is not None and not self._finished:
            seat = Gdk.Display.get_default().get_default_seat()
            self._grabbed = seat.grab(gdk_window, Gdk.SeatCapabilities.KEYBOARD, True, None,
                                      None, None, None) == Gdk.GrabStatus.SUCCESS
        return False

    # -- the end
    def finish(self) -> None:
        png = marked_png(self._shot, self.strokes) if self.strokes else None
        self._end(png)

    def cancel(self) -> None:
        self._end(None)

    def _end(self, png: bytes | None) -> None:
        if self._finished:
            return
        self._finished = True
        if self._grabbed:
            Gdk.Display.get_default().get_default_seat().ungrab()
        self.canvas.destroy()
        self.bar.destroy()
        self._owner._marker = None
        self._done(png)
