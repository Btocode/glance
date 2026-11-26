"""GtkOverlay: the status icon, the suggestion strip and its drawer (needs a live X session).

All three are `Gtk.WindowType.POPUP` windows with `accept_focus` off: the window manager never
manages them, so they cannot take the keyboard focus from the app being written in (findings
S5.1). The strip lets clicks through everywhere but its two buttons; the status icon everywhere
but its own circle. Everything is built from
the pure views; no text is kept here beyond what is on screen.

The icon, the strip and the drawer follow the window they belong to: when it moves they move with it
in the same frame (Wnck reports the new geometry); while it is being resized they hide, and
come back placed by the fresh field rectangles capture then sends, ~150 ms after it settles.

The one window that does take the focus is the "Rewrite as…" entry (`ask_text`): typing
needs it. It gives the focus back to the window that had it before it reports the answer.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

import cairo
import gi

gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango

from glance.diffing import diff_markup
from glance.grammar.harper import SPELL_RULE
from glance.log import log_event
from glance.protocol import Rect
from glance.ui import look
from glance.ui import selection_bar as sb
from glance.ui.chip_model import ChipView
from glance.ui.hover_menu import CLOSE_MS as MENU_CLOSE_MS
from glance.ui.hover_menu import OPEN_MS as MENU_OPEN_MS
from glance.ui.hover_menu import HoverMenu
from glance.ui.panel_model import (
    IssueRow,
    PanelView,
    ResultView,
    SuggestedBox,
    ToneChip,
    suggested_box,
)
from glance.ui.placement import Anchor, clamp_to_monitor, place_status, place_strip
from glance.ui.status_model import StatusState, StatusView
from glance.ui.strip_model import StripView, scroll_max_h
from glance.ui.theme import glow_path, install, mark_path, rgb, rgba

ICON = 22  # the status icon's click target: an invisible circle around the mark
# Its window: the circle at (ICON_X, ICON_Y), with room above and right for the count badge.
ICON_X, ICON_Y = 1, 5
ICON_BOX = (30, 28)
BADGE_C, BADGE_R = (23.0, 7.0), 7.0  # the badge's cut-out disc
SPIN_MS = look.FRAME_MS  # the working animation's frame time, only while working
DRAWER_MIN_W = 520
SEL_W = 460  # select-anything's panel
GAP = 4  # between the strip and the drawer
SHADOW = (10, 4, 10, 16)  # left, top, right, bottom room for the drawer's soft shadow
NO_SHADOW = (0, 0, 0, 0)  # without a compositor nothing is see-through: no room, no shadow
_LIST_MAX_H = 240
_MAX_CHOICES = 5
_LINE_FIXES = 3  # fixes listed on the drawer's one muted line; the rest behind "+N more"
_PREVIEW_MAX_H = 150
ASK_RETURN_MS = 1500  # how long the previous window gets to be the active one again
_ASK_POLL_MS = 20
SETTLE_MS = 150  # a resizing window is still for this long: show the strip again
RESHOW_MS = 400  # no fresh field rectangles by then: show it where it was, moved along
ASK_PLACEHOLDER = "e.g. make it a bullet list"
MARK_PX = 18  # the brand mark in the status icon's click target
_SUGGEST_MAX_H = 200  # the panel's suggested text scrolls past this
_PIXBUFS: dict[str, GdkPixbuf.Pixbuf] = {}


def _badge_r(badge: str) -> float:
    """The count badge's radius; the tone-only dot's."""
    return 3.5 if badge == "•" else BADGE_R - 0.8


def _popup(type_hint: Gdk.WindowTypeHint) -> Gtk.Window:
    win = Gtk.Window(type=Gtk.WindowType.POPUP)
    win.set_accept_focus(False)
    win.set_focus_on_map(False)
    win.set_type_hint(type_hint)
    win.set_keep_above(True)
    win.set_skip_taskbar_hint(True)
    win.set_skip_pager_hint(True)
    win.set_resizable(False)
    visual = win.get_screen().get_rgba_visual()
    if visual is not None:
        win.set_visual(visual)
    win.set_app_paintable(True)
    win.get_style_context().add_class("glance-root")
    return win


class _Fit(Gtk.Box):
    """A box exactly `fit_w` px wide, however wide its content would like to be: a popup
    window takes its child's natural width, and the strip must be the field's width."""

    def __init__(self) -> None:
        super().__init__()
        self.fit_w = 0

    def do_get_preferred_width(self) -> tuple[int, int]:
        if self.fit_w:
            return self.fit_w, self.fit_w
        return Gtk.Box.do_get_preferred_width(self)


def _rounded(x: int, y: int, w: int, h: int, r: int) -> cairo.Region:
    """A rounded rectangle as a region (rows of rectangles): the window's outline where
    there is no compositor to make its corners see-through."""
    region = cairo.Region()
    r = max(0, min(r, w // 2, h // 2))
    for i in range(r):
        inset = r - round((r * r - (r - i - 0.5) ** 2) ** 0.5)
        for row in (y + i, y + h - 1 - i):
            region.union(cairo.RectangleInt(x + inset, row, w - 2 * inset, 1))
    if h > 2 * r:
        region.union(cairo.RectangleInt(x, y + r, w, h - 2 * r))
    return region


def _pointer(widget: Gtk.Widget) -> Gtk.Widget:
    """The hand cursor while the pointer is on `widget` (a button, the icon); text and
    empty space keep the default one."""
    def enter(w: Gtk.Widget, _e: Any) -> bool:
        win = w.get_window()
        if win is not None:
            win.set_cursor(Gdk.Cursor.new_from_name(win.get_display(), "pointer"))
        return False

    def leave(w: Gtk.Widget, _e: Any) -> bool:
        win = w.get_window()
        if win is not None:
            win.set_cursor(None)
        return False

    widget.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
    widget.connect("enter-notify-event", enter)
    widget.connect("leave-notify-event", leave)
    return widget


def _raise(win: Gtk.Window) -> None:
    """On top of the windows mapped since it was made (an override-redirect window keeps
    its place in the stack when it is mapped again). Raising never moves the focus."""
    gdk_window = win.get_window()
    if gdk_window is not None:
        gdk_window.raise_()


def _see_through(screen: Gdk.Screen | None) -> bool:
    """A compositor runs and the screen has an RGBA visual: what a window leaves clear
    (its corners, the drawer's shadow margins) shows what is under it. Otherwise that
    would be black, so windows are cut to their cards instead."""
    return bool(screen is not None and screen.is_composited()
                and screen.get_rgba_visual() is not None)


def _clear(_widget: Gtk.Widget, cr: Any) -> bool:
    """Every draw starts from a fully transparent window (margins included)."""
    cr.save()
    cr.reset_clip()
    cr.set_source_rgba(0, 0, 0, 0)
    cr.set_operator(cairo.OPERATOR_SOURCE)
    cr.paint()
    cr.restore()
    cr.set_operator(cairo.OPERATOR_OVER)
    return False


def _classes(widget: Gtk.Widget, *names: str) -> Gtk.Widget:
    for name in names:
        widget.get_style_context().add_class(name)
    return widget


def _label(text: str, *classes: str, wrap: bool = True, chars: int = 60) -> Gtk.Label:
    label = Gtk.Label(xalign=0, wrap=wrap, max_width_chars=chars, selectable=False)
    label.set_text(text)  # never markup: this is the user's text
    if wrap:
        label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
    return _classes(label, *classes)


def _skeleton(width: int) -> Gtk.Widget:
    """A loading placeholder bar (`look.SKELETON_*`)."""
    bar = _classes(Gtk.Box(), "glance-skeleton")
    bar.set_size_request(width, look.SKELETON_BAR_H)
    bar.set_halign(Gtk.Align.START)
    return bar


class _Spinner(Gtk.DrawingArea):
    """The loader's spinner: the working icon's arc (`look.ARC_*`), 12 px, turning once a
    cycle. Its timer runs only while it is on screen."""

    def __init__(self, colour: Callable[[], tuple[float, float, float]]) -> None:
        super().__init__()
        self._colour = colour
        self._phase = 0
        self._src: int | None = None
        self.set_size_request(12, 12)
        self.set_valign(Gtk.Align.CENTER)
        self.connect("draw", self._draw)
        self.connect("map", lambda *_a: self._run(True))
        self.connect("unmap", lambda *_a: self._run(False))

    def _run(self, on: bool) -> None:
        if on and self._src is None:
            self._src = GLib.timeout_add(look.FRAME_MS, self._tick)
        elif not on and self._src is not None:
            GLib.source_remove(self._src)
            self._src = None

    def _tick(self) -> bool:
        self._phase = (self._phase + 1) % look.FRAMES
        self.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _draw(self, _w: Gtk.Widget, cr: Any) -> bool:
        a = 2 * math.pi * self._phase / look.FRAMES
        cr.set_line_width(1.6)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_source_rgba(*self._colour(), 0.25)
        cr.arc(6, 6, 4.6, 0, 2 * math.pi)
        cr.stroke()
        cr.set_source_rgb(*self._colour())
        cr.arc(6, 6, 4.6, a, a + look.ARC_SWEEP)
        cr.stroke()
        return False


def _loader(text: str, colour: Callable[[], tuple[float, float, float]]) -> Gtk.Widget:
    """A small spinner and "Checking with Claude…", muted."""
    row = Gtk.Box(spacing=6)
    spinner = _Spinner(colour)
    row.pack_start(spinner, False, False, 0)
    row.pack_start(_label(text, "glance-meta", wrap=False), False, False, 0)
    row.set_name("loader")
    return row


def _escape(text: str) -> str:
    return GLib.markup_escape_text(text, -1)


def _pixbuf(path: str) -> GdkPixbuf.Pixbuf:
    if path not in _PIXBUFS:
        _PIXBUFS[path] = GdkPixbuf.Pixbuf.new_from_file(path)
    return _PIXBUFS[path]


def paint_mark(cr: Any, widget: Gtk.Widget, x: float, y: float, px: int,
               alpha: float = 1.0, tint: str | None = None) -> None:
    """The brand mark, `px` wide at (x, y): the file for the screen's scale, so it is crisp.
    `tint`: its silhouette in that one colour instead (the muted grey)."""
    scale = max(1, widget.get_scale_factor())
    pb = _pixbuf(mark_path(px * scale))
    cr.save()
    cr.translate(x, y)
    cr.scale(px / pb.get_width(), px / pb.get_height())
    Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
    pattern = cr.get_source()
    pattern.set_filter(cairo.FILTER_GOOD)
    if tint is None:
        cr.paint_with_alpha(alpha)
    else:
        cr.set_source_rgba(*rgba(tint)[:3], alpha)
        cr.mask(pattern)
    cr.restore()


def paint_glow(cr: Any, widget: Gtk.Widget, x: float, y: float, px: int, colour: str,
               strength: float = 1.0) -> None:
    """The soft glow behind a mark `px` wide at (x, y), in `colour` (an rgba token) times
    `strength`."""
    scale = max(1, widget.get_scale_factor())
    mark = _pixbuf(mark_path(px * scale))
    pb = _pixbuf(glow_path(px * scale))
    k = px / mark.get_width()
    side = pb.get_width() * k
    cr.save()
    cr.translate(x + px / 2 - side / 2, y + px / 2 - side / 2)
    cr.scale(k, k)
    Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
    pattern = cr.get_source()
    pattern.set_filter(cairo.FILTER_GOOD)
    r, g, b, a = rgba(colour)
    cr.set_source_rgba(r, g, b, max(0.0, min(1.0, a * strength)))
    cr.mask(pattern)
    cr.restore()


def mark_region(widget: Gtk.Widget, x: int, y: int, px: int) -> cairo.Region:
    """Where the mark `px` wide at (x, y) is drawn, as a region: the icon's outline where
    there is no compositor to make the rest of its window see-through."""
    scale = max(1, widget.get_scale_factor())
    pb = _pixbuf(mark_path(px * scale)).scale_simple(px, px, GdkPixbuf.InterpType.BILINEAR)
    data, stride, n = pb.get_pixels(), pb.get_rowstride(), pb.get_n_channels()
    region = cairo.Region()
    for row in range(px):
        run = None
        for col in range(px + 1):
            on = col < px and n == 4 and data[row * stride + col * n + 3] > 60
            if on and run is None:
                run = col
            elif not on and run is not None:
                region.union(cairo.RectangleInt(x + run, y + row, col - run, 1))
                run = None
    return region


def arc_region(cx: float, cy: float, start: float) -> cairo.Region:
    """Where the working arc (`look.ARC_*`) starting at `start` is drawn, as a region: part
    of the icon's outline where there is no compositor."""
    region = cairo.Region()
    steps = 24
    half = look.ARC_WIDTH / 2 + 0.6
    for k in range(steps + 1):
        t = start + look.ARC_SWEEP * k / steps
        x = cx + look.ARC_RADIUS * math.cos(t)
        y = cy + look.ARC_RADIUS * math.sin(t)
        region.union(cairo.RectangleInt(int(x - half), int(y - half),
                                        int(2 * half + 1), int(2 * half + 1)))
    return region


def glyph_path(cr: Any, name: str, x: float, y: float, px: float) -> None:
    """One of `look.GLYPHS` as the current path, `px` square at (x, y)."""
    k = px / 16
    for cmd, *pts in look.GLYPHS[name]:
        xy = [(x + pts[i] * k, y + pts[i + 1] * k) for i in range(0, len(pts), 2)]
        if cmd == "M":
            cr.move_to(*xy[0])
        elif cmd == "L":
            cr.line_to(*xy[0])
        elif cmd == "C":
            cr.curve_to(*xy[0], *xy[1], *xy[2])
        else:
            cr.close_path()


def _mark(px: int) -> Gtk.Widget:
    area = Gtk.DrawingArea()
    area.set_size_request(px, px)
    area.set_valign(Gtk.Align.CENTER)

    def draw(w: Gtk.Widget, cr: Any) -> bool:
        paint_mark(cr, w, 0, 0, px)
        return True

    area.connect("draw", draw)
    return area


def _kbd(text: str) -> Gtk.Label:
    """A keycap: a small bordered label (Alt+Enter, Esc, 1-4, the return key, Tab)."""
    label = _classes(Gtk.Label(label=text, selectable=False), "glance-kbd")
    label.set_valign(Gtk.Align.CENTER)
    return label


def _keycaps(keys: list[str], plus: bool = False) -> Gtk.Widget:
    """A chord as keycaps, one per key (with a "+" between them when `plus`)."""
    row = Gtk.Box(spacing=4)
    for i, key in enumerate(keys):
        if i and plus:
            row.pack_start(_classes(Gtk.Label(label="+"), "glance-plus"), False, False, 0)
        row.pack_start(_kbd(key), False, False, 0)
    return row


def button_text(button: Gtk.Button) -> str:
    """What a button says: its label, or the text of the labels inside it (tone cards)."""
    if button.get_label():
        return button.get_label()
    out: list[str] = []

    def walk(w: Gtk.Widget) -> None:
        if isinstance(w, Gtk.Label):
            out.append(w.get_text())
        elif isinstance(w, Gtk.Container):
            for c in w.get_children():
                walk(c)

    walk(button)
    return " | ".join(t for t in out if t)


class GtkOverlay:
    """OverlayPort. `on_panel_action(action, args)` gets, from the strip, use and more; from
    the drawer accept{issue_id,choice}, fix_all, add_dict{issue_id}, ignore{issue_id,scope},
    tone{key} (a click on a tone card: select it, or unselect it; "custom": "Rewrite as…"),
    profile{name}, copy, close and guard_silence{scope} (the send-guard banner's buttons); in
    a select-anything panel (`mode == "selection"`) tone{key}, sel_copy, sel_paste and close.
    Nothing is asked for while the pointer only rests on something."""

    def __init__(self, on_chip_click: Callable[[], None],
                 on_panel_action: Callable[[str, dict[str, Any]], None],
                 hover_menu: Callable[[], HoverMenu | None] = lambda: None,
                 use_key: Callable[[], str] = lambda: "Alt+Return") -> None:
        self._on_chip_click = on_chip_click
        self._use_key = use_key
        self._hover_menu = hover_menu  # the icon's hover menu now (None: not now)
        self._menu_view: HoverMenu | None = None
        self._menu_open_src: int | None = None
        self._menu_close_src: int | None = None
        self._in_icon = self._in_menu = False
        self._act = on_panel_action
        self._theme = install()
        self._theme.on_change(self._restyle)
        screen = Gdk.Screen.get_default()
        self._composited = _see_through(screen)
        self._shadow = SHADOW if self._composited else NO_SHADOW
        self._status_view: StatusView | None = None
        self._status_xy: tuple[int, int] | None = None
        self._spin: int | None = None  # GLib source turning the working ring
        self._phase = 0
        self._strip_view: StripView | None = None
        self._anchor: Anchor | None = None  # the field's, as last told (or moved along)
        self._strip_rect: tuple[int, int, int, int] | None = None
        self._strip_w = 0  # the width its content was built for
        self._strip_buttons: list[Gtk.Widget] = []
        self._panel_view: PanelView | None = None
        self._panel_anchor: Anchor | None = None  # None: select-anything, at a point
        self._panel_point = (0, 0)
        self._panel_xy: tuple[int, int] | None = None
        self._panel_size = (DRAWER_MIN_W, 1)
        self._panel_w = 0
        self._profiles_open = False
        self._details_open = False
        self._ask_input: Gtk.Widget | None = None  # the drawer's "Rewrite as…" field
        self._ask: dict[str, Any] | None = None  # the open "Rewrite as…" entry
        self._ask_wait: int | None = None  # GLib source polling for the focus to return
        self._wnck_screen: Any = None
        self._followed: Any = None  # the active window, whose geometry is followed
        self._follow_id = 0
        self._geometry: tuple[int, int, int, int] | None = None
        self._resizing = False
        self._settle: int | None = None
        self._reshow: int | None = None
        self._fresh = False  # a show_strip arrived since the resize began

        self._icon = _popup(Gdk.WindowTypeHint.TOOLTIP)
        self._icon.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self._icon.connect("draw", self._draw_icon)
        self._icon.connect("button-press-event", self._icon_pressed)
        _pointer(self._icon)
        self._icon.connect("enter-notify-event", lambda *_a: self._menu_enter("icon"))
        self._icon.connect("leave-notify-event", lambda *_a: self._menu_leave("icon"))

        # The hover menu: like the strip, a popup that never takes the focus.
        self._menu = _popup(Gdk.WindowTypeHint.TOOLTIP)
        self._menu.connect("draw", _clear)
        self._menu.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self._menu.connect("enter-notify-event", lambda *_a: self._menu_enter("menu"))
        self._menu.connect("leave-notify-event", self._menu_left)
        self._icon.connect("map", lambda *_a: self._icon_shape())
        self._icon.set_size_request(*ICON_BOX)

        self._strip = _popup(Gdk.WindowTypeHint.TOOLTIP)
        self._strip.connect("draw", _clear)
        self._strip.connect("size-allocate", lambda *_a: self._strip_shape())
        self._strip.connect("map", lambda *_a: self._strip_shape())

        # Selection actions: the mark beside selected text, then its action bar. A popup
        # that never takes the focus, like the strip.
        self._sel = _popup(Gdk.WindowTypeHint.TOOLTIP)
        self._sel.connect("draw", _clear)
        self._sel.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self._sel.connect("enter-notify-event", lambda *_a: self._sel_crossing(True, None))
        self._sel.connect("leave-notify-event", lambda _w, e: self._sel_crossing(False, e))
        self._sel_view: sb.SelectionBarView | None = None
        self._sel_xy = (0, 0)

        self._panel = _popup(Gdk.WindowTypeHint.POPUP_MENU)
        self._panel.connect("draw", _clear)
        self._panel.connect("size-allocate", lambda *_a: self._panel_shape())
        self._panel.connect("map", lambda *_a: self._panel_shape())

        screen = Gdk.Screen.get_default()
        if screen is not None:
            screen.connect("monitors-changed", lambda *_a: (self._layout(),
                                                            self._place_icon()))
            screen.connect("composited-changed", lambda *_a: self._composited_changed())
        try:
            wnck = self._screen()
            wnck.connect("active-window-changed", lambda *_a: self._follow_active())
            self._follow_active()
        except Exception:  # noqa: BLE001 - no Wnck: the strip still follows each snapshot
            self._wnck_screen = None

    # ------------------------------------------------------------------ OverlayPort

    def show_chip(self, view: ChipView, x: int, y: int) -> None:
        """The chip's state: nothing is drawn for it any more (the status icon is)."""

    def hide_chip(self) -> None:
        pass

    def show_status(self, view: StatusView, anchor: Anchor) -> None:
        if view.state is StatusState.HIDDEN:
            self.hide_status()
            return
        self._anchor = anchor
        if view != self._status_view:
            self._status_view = view
            self._icon_shape()
            self._icon.queue_draw()
        self._place_icon()
        self._animate()

    def hide_status(self) -> None:
        self._hide_menu()
        if self._icon.get_visible():
            self._icon.hide()
        self._status_xy = None
        self._status_view = None
        self._animate()

    def show_strip(self, view: StripView, anchor: Anchor) -> None:
        self._fresh = True
        rebuilt = view != self._strip_view
        if rebuilt:
            self._strip_view = view
            self._build_strip(view)
            if self._field_drawer() and self._panel_view is not None:
                self._rebuild_panel(self._panel_view)  # it shows the corrected text
        self._anchor = anchor
        self._layout(force=rebuilt)

    def hide_strip(self) -> None:
        had = self._strip_view is not None
        self._strip_view = None
        self._strip_rect = None
        if self._strip.get_visible():
            self._strip.hide()
        if had and self._field_drawer() and self._panel_view is not None:
            self._rebuild_panel(self._panel_view)  # without the corrected text now
            self._layout(force=True)

    def show_panel(self, view: PanelView, x: int, y: int, anchor: Anchor | None = None) -> None:
        mode_changed = (anchor is None) != (self._panel_anchor is None) \
            or self._panel_view is None
        self._panel_anchor = anchor
        self._panel_point = (x, y)
        if anchor is not None:
            self._anchor = anchor
        if view != self._panel_view or mode_changed:
            self._panel_view = view
            self._rebuild_panel(view)
        self._layout(force=True)

    def hide_panel(self) -> None:
        if self._panel.get_visible():
            self._panel.hide()
        self._panel_xy = None
        self._panel_view = None
        self._panel_anchor = None
        self._profiles_open = False
        self._details_open = False
        self._ask_input = None
        if self._strip_view is not None:
            self._layout()

    def monitors(self) -> list[Rect]:
        display = Gdk.Display.get_default()
        out = []
        for i in range(display.get_n_monitors()):
            g = display.get_monitor(i).get_geometry()
            out.append(Rect(g.x, g.y, g.width, g.height))
        return out

    def copy_to_clipboard(self, text: str) -> None:
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(text, -1)
        clipboard.store()

    # ------------------------------------------------------------------ selection actions

    def show_selmark(self, view: sb.SelectionBarView, x: int, y: int) -> None:
        """The mark at (x, y), or the action bar growing from there (kept on the monitor)."""
        rebuilt = view != self._sel_view
        if rebuilt:
            self._sel_view = view
            self._build_sel(view)
        child = self._sel.get_child()
        w, h = child.get_preferred_size()[1].width, child.get_preferred_size()[1].height
        if view.state == "mark":
            w, h = sb.MARK_BOX
        xy = sb.place_bar((x, y), (w, h), self.monitors())
        self._sel_xy = xy
        self._sel.resize(w, h)
        self._sel.move(*xy)
        if not self._sel.get_visible():
            self._sel.show_all()
            self._sel.move(*xy)
        self._sel_shape(w, h)
        _raise(self._sel)

    def hide_selmark(self) -> None:
        self._sel_view = None
        if self._sel.get_visible():
            self._sel.hide()

    def selmark_hit(self, x: int, y: int) -> bool:
        """Is (x, y) on the mark or the bar? (A click there must not hide it.)"""
        if not self._sel.get_visible():
            return False
        sx, sy = self._sel_xy
        w, h = self._sel.get_size()
        return sx <= x < sx + w and sy <= y < sy + h

    def selmark_geometry(self) -> tuple[int, int, int, int] | None:
        """Test access: where the mark or the bar is, None while hidden."""
        if not self._sel.get_visible():
            return None
        return (*self._sel_xy, *self._sel.get_size())

    def selmark_state(self) -> str | None:
        return None if self._sel_view is None else self._sel_view.state

    def selmark_window(self) -> Gtk.Window:
        return self._sel

    def _sel_crossing(self, inside: bool, event: Gdk.EventCrossing | None) -> bool:
        if event is not None and event.detail == Gdk.NotifyType.INFERIOR:
            return False  # onto one of its own buttons: still inside
        self._act("sel_hover", {"inside": inside})
        return False

    def _sel_shape(self, w: int, h: int) -> None:
        gdk_window = self._sel.get_window()
        if gdk_window is None or self._composited:
            return
        gdk_window.shape_combine_region(_rounded(0, 0, w, h, 9 if w > sb.MARK_BOX[0] else 13),
                                        0, 0)

    def _build_sel(self, view: sb.SelectionBarView) -> None:
        old = self._sel.get_child()
        if old is not None:
            self._sel.remove(old)
            old.destroy()
        if view.state == "mark":
            child = self._sel_mark()
        else:
            child = self._sel_bar(view)
        child.show_all()
        self._sel.add(child)

    def _sel_mark(self) -> Gtk.Widget:
        """The bare mark on a small round surface, a hairline round it: visible on any page."""
        area = _pointer(Gtk.Button(can_focus=False, focus_on_click=False))
        _classes(area, "glance-selmark")
        area.set_size_request(*sb.MARK_BOX)
        draw = Gtk.DrawingArea()
        draw.set_size_request(*sb.MARK_BOX)
        area.add(draw)

        def paint(w: Gtk.Widget, cr: Any) -> bool:
            t = self._theme.tokens
            bw, bh = sb.MARK_BOX
            r = bw / 2 - 1
            cr.arc(bw / 2, bh / 2, r, 0, 6.2832)
            cr.set_source_rgb(*rgb(t, "surface"))
            cr.fill_preserve()
            cr.set_source_rgba(*rgba(t["use_border"]))
            cr.set_line_width(1.2)
            cr.stroke()
            px = sb.MARK_PX - 2
            paint_mark(cr, w, (bw - px) / 2, (bh - px) / 2, px)
            return True

        draw.connect("draw", paint)
        area.connect("clicked", lambda _b: self._act("sel_mark", {}))
        area.set_tooltip_text("Glance: act on the selected text")
        return area

    def _sel_bar(self, view: sb.SelectionBarView) -> Gtk.Widget:
        """The mark, then one button per action (`⋯` opens a second row: "Translate to…"
        and the audience's tones)."""
        outer = _classes(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4),
                         "glance-menu", "glance-selbar")
        row = Gtk.Box(spacing=2)
        lead = _mark(16)
        lead.set_margin_start(4)
        lead.set_margin_end(4)
        row.pack_start(lead, False, False, 0)
        for a in view.actions:
            row.pack_start(self._sel_button(a), False, False, 0)
        outer.pack_start(row, False, False, 0)
        if view.more:
            more = Gtk.Box(spacing=2)
            for a in view.more:
                more.pack_start(self._sel_button(a), False, False, 0)
            outer.pack_start(_classes(more, "glance-selmore"), False, False, 0)
        return outer

    def _sel_button(self, a: sb.BarAction) -> Gtk.Button:
        action, args = ("sel_more", {}) if a.key == "more" else ("sel_action", {"key": a.key})
        button = _pointer(Gtk.Button(can_focus=False, focus_on_click=False))
        _classes(button, "glance-item", "glance-selbtn")
        inner = Gtk.Box(spacing=5)
        if a.glyph is not None and a.glyph in look.GLYPHS:
            inner.pack_start(self._glyph(a.glyph, 13), False, False, 0)
        inner.pack_start(_label(a.label, wrap=False), False, False, 0)
        button.add(inner)
        if a.tip:
            button.set_tooltip_text(a.tip)
        button.connect("clicked", lambda _b: self._act(action, dict(args)))
        return button

    # ------------------------------------------------------------------ placing

    def _place_icon(self) -> None:
        """The icon at the right end of the caret line; hidden while the window resizes."""
        if self._status_view is None or self._anchor is None or self._resizing:
            return
        mons = self.monitors()
        at = place_status(self._anchor, ICON, mons)
        if at is None:
            if self._icon.get_visible():
                self._icon.hide()
            self._status_xy = None
            return
        xy = clamp_to_monitor(at[0] - ICON_X, at[1] - ICON_Y, ICON_BOX, mons)
        if xy != self._status_xy or not self._icon.get_visible():
            self._status_xy = xy
            self._icon.move(*xy)
        if not self._icon.get_visible():
            self._icon.show()
            self._icon.move(*xy)  # a place set while unmapped can be lost
            _raise(self._icon)
            self._icon_shape()

    def _icon_shape(self) -> None:
        """Clicks reach the icon only on its (invisible) circle and badge; the rest goes
        through. Without a compositor nothing is see-through: the window is cut to the mark
        and the badge (which also leaves only them to click)."""
        gdk_window = self._icon.get_window()
        if gdk_window is None:
            return
        region = _rounded(ICON_X, ICON_Y, ICON, ICON, ICON // 2)
        badge = cairo.Region()
        view = self._status_view
        if view is not None and view.badge is not None:
            d = int(BADGE_R * 2)
            region.union(_rounded(int(BADGE_C[0] - BADGE_R), int(BADGE_C[1] - BADGE_R),
                                  d, d, d // 2))
            r = round(_badge_r(view.badge))
            badge = _rounded(int(BADGE_C[0]) - r, int(BADGE_C[1]) - r, 2 * r, 2 * r, r)
        gdk_window.input_shape_combine_region(region, 0, 0)
        if not self._composited:
            m = (ICON - MARK_PX) // 2
            seen = mark_region(self._icon, ICON_X + m, ICON_Y + m, MARK_PX)
            seen.union(badge)
            lk = None if view is None else look.icon_look(view.state.value, view.spin,
                                                          self._phase)
            if lk is not None and lk.arc is not None:  # the working arc shows too
                seen.union(arc_region(ICON_X + ICON / 2, ICON_Y + ICON / 2, lk.arc))
            gdk_window.shape_combine_region(seen, 0, 0)

    def _animate(self) -> None:
        """Let the working glow breathe while the icon shows a spinning WORKING; no timer
        otherwise (a still glow while keys keep arriving)."""
        view = self._status_view
        want = (view is not None and view.state is StatusState.WORKING and view.spin
                and self._icon.get_visible())
        if want and self._spin is None:
            self._spin = GLib.timeout_add(SPIN_MS, self._tick)
        elif not want and self._spin is not None:
            GLib.source_remove(self._spin)
            self._spin = None

    def _tick(self) -> bool:
        view = self._status_view
        if view is None or view.state is not StatusState.WORKING or not view.spin:
            self._spin = None
            return GLib.SOURCE_REMOVE
        self._phase = (self._phase + 1) % look.FRAMES
        if not self._composited:
            self._icon_shape()  # the arc moved: so does the window's outline
        self._icon.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _field_drawer(self) -> bool:
        return self._panel_view is not None and self._panel_anchor is not None

    def _layout(self, force: bool = False) -> None:
        """Put the strip and the field's drawer where the anchor says, one above the other.

        The drawer goes under the strip; when the two do not fit below the line they go
        above it, the strip nearest the line. Nothing is shown while the window resizes.
        While the field's panel is open it shows the strip's corrected text itself
        ("Suggested"): the strip is not shown on its own then.
        """
        mons = self.monitors()
        if self._panel_view is not None and self._panel_anchor is None:
            self._place_selection(mons)
        drawer = self._field_drawer()
        strip = self._strip_view is not None and not drawer
        if drawer and self._strip.get_visible():
            self._strip.hide()
            self._strip_rect = None
        anchor = self._anchor
        if not (strip or drawer) or anchor is None:
            return
        first = place_strip(anchor, 1, mons)
        if first is None:
            self._strip.hide()
            if drawer:
                self._panel.hide()
            return
        mon = next((m for m in mons if m.x <= first.x < m.x + m.w), mons[0] if mons else None)
        strip_h = self._size_strip(first.w, force) if strip else 0
        card_w = first.w
        if drawer:
            card_w = min(max(first.w, DRAWER_MIN_W), mon.w if mon else first.w)
            card_h = self._size_drawer(card_w, force)
        else:
            card_h = 0
        total = strip_h + (GAP if strip and drawer else 0) + card_h
        p = place_strip(anchor, total, mons)
        if p is None:
            return
        if p.above:
            card_y, strip_y = p.y, p.y + card_h + (GAP if strip else 0)
        else:
            strip_y, card_y = p.y, p.y + strip_h + (GAP if strip else 0)
        if self._resizing:
            return  # placed again once the window is still
        if strip:
            rect = (p.x, strip_y, p.w, strip_h)
            if rect != self._strip_rect or not self._strip.get_visible():
                self._strip_rect = rect
                self._strip.move(p.x, strip_y)
            if not self._strip.get_visible():
                self._strip.show_all()
                self._strip.resize(p.w, strip_h)  # not its natural width: the field's
                self._strip.move(p.x, strip_y)
                _raise(self._strip)
        if drawer:
            card_x = p.x
            if mon is not None:
                card_x = max(mon.x, min(card_x, mon.x + mon.w - card_w))
            xy = (card_x - self._shadow[0], card_y - self._shadow[1])
            if xy != self._panel_xy or not self._panel.get_visible():
                self._panel_xy = xy
                self._panel.move(*xy)
            if not self._panel.get_visible():
                self._panel.show_all()
                self._panel.resize(*self._panel_size)
                self._panel.move(*xy)  # a size or place set while unmapped can be lost
                _raise(self._panel)
            self._panel_shape()
        if strip:
            self._strip_shape()

    def _size_strip(self, width: int, force: bool) -> int:
        child = self._strip.get_child()
        if child is None:
            return 1
        if width != self._strip_w or force:
            self._strip_w = width
            child.fit_w = width
            child.queue_resize()
            height = max(1, child.get_preferred_height_for_width(width)[1])
            self._strip.set_size_request(width, height)
            self._strip.resize(width, height)
        return self._strip.get_size_request()[1]

    def _size_drawer(self, card_w: int, force: bool) -> int:
        outer = self._panel.get_child()
        if outer is None:
            return 1
        if card_w != self._panel_w or force:
            self._panel_w = card_w
            card = outer.get_children()[0]
            outer.fit_w = card_w  # its margins (the shadow's room) come on top
            outer.queue_resize()
            card_h = card.get_preferred_height_for_width(card_w)[1]
            sh = self._shadow
            size = (card_w + sh[0] + sh[2], card_h + sh[1] + sh[3])
            self._panel_size = size
            self._panel.set_size_request(*size)
            self._panel.resize(*size)
        return self._panel_size[1] - self._shadow[1] - self._shadow[3]

    def _place_selection(self, mons: list[Rect]) -> None:
        h = self._size_drawer(SEL_W, False) + self._shadow[1] + self._shadow[3]
        x, y = self._panel_point
        sh = self._shadow
        xy = clamp_to_monitor(x - sh[0], y - sh[1], (self._panel_size[0], h), mons)
        if xy != self._panel_xy or not self._panel.get_visible():
            self._panel_xy = xy
            self._panel.move(*xy)
        if not self._panel.get_visible():
            self._panel.show_all()
            self._panel.resize(*self._panel_size)
            self._panel.move(*xy)
            _raise(self._panel)
        self._panel_shape()

    def _composited_changed(self) -> None:
        """A compositor started or stopped: the drawer gains or loses its shadow margins,
        and every window its cut-out outline (else its clear parts would show black)."""
        now = _see_through(Gdk.Screen.get_default())
        if now == self._composited:
            return
        self._composited = now
        self._shadow = SHADOW if now else NO_SHADOW
        log_event("overlay_composited", on=now)
        for win in (self._icon, self._strip, self._panel, self._menu, self._sel):
            gdk_window = win.get_window()
            if gdk_window is not None and now:
                gdk_window.shape_combine_region(None, 0, 0)
            win.queue_draw()
        self._icon_shape()
        if self._panel_view is not None:
            self._rebuild_panel(self._panel_view)
        if self._strip_view is not None:
            self._build_strip(self._strip_view)
        if self._sel_view is not None:  # the mark's round surface: cut out again, or not
            w, h = self._sel.get_size()
            self._sel_shape(w, h)
        self._layout(force=True)

    def _strip_shape(self) -> None:
        """Clicks go through the strip except on its buttons."""
        gdk_window = self._strip.get_window()
        if gdk_window is None:
            return
        region = cairo.Region()
        for button in self._strip_buttons:
            if button.get_mapped():
                a = button.get_allocation()
                x, y = button.translate_coordinates(self._strip, 0, 0) or (a.x, a.y)
                region.union(cairo.RectangleInt(x, y, a.width, a.height))
        gdk_window.input_shape_combine_region(region, 0, 0)
        if not self._composited and self._strip_rect is not None:
            _x, _y, w, h = self._strip_rect
            gdk_window.shape_combine_region(_rounded(0, 0, w, h, 9), 0, 0)

    def _panel_shape(self) -> None:
        """The drawer's shadow lets clicks through; the card takes them."""
        gdk_window = self._panel.get_window()
        if gdk_window is None:
            return
        w, h = self._panel_size
        sh = self._shadow
        card = (sh[0], sh[1], w - sh[0] - sh[2], h - sh[1] - sh[3])
        gdk_window.input_shape_combine_region(cairo.Region(cairo.RectangleInt(*card)), 0, 0)
        if not self._composited:
            gdk_window.shape_combine_region(_rounded(*card, 12), 0, 0)

    # ------------------------------------------------------------------ following the window

    def _screen(self) -> Any:
        if self._wnck_screen is None:
            gi.require_version("Wnck", "3.0")
            from gi.repository import Wnck

            Wnck.set_client_type(Wnck.ClientType.PAGER)  # activation requests are honoured
            self._wnck_screen = Wnck.Screen.get_default()
        self._wnck_screen.force_update()
        return self._wnck_screen

    def _follow_active(self) -> None:
        screen = self._wnck_screen
        win = screen.get_active_window() if screen is not None else None
        if win is self._followed:
            return
        if self._followed is not None and self._follow_id:
            try:
                self._followed.disconnect(self._follow_id)
            except TypeError:
                pass
        self._followed, self._follow_id = win, 0
        self._geometry = None
        if win is not None:
            self._geometry = tuple(win.get_geometry())
            self._follow_id = win.connect("geometry-changed", self._on_geometry)

    def _on_geometry(self, win: Any) -> None:
        new = tuple(win.get_geometry())
        old, self._geometry = self._geometry, new
        anchor = self._anchor
        if old is None or anchor is None or not anchor.pid or int(win.get_pid()) != anchor.pid:
            return
        dx, dy = new[0] - old[0], new[1] - old[1]
        if new[2:] == old[2:]:
            if dx or dy:
                self._anchor = anchor.moved(dx, dy)
                if self._panel_anchor is not None:
                    self._panel_anchor = self._anchor
                self._layout()  # this frame: the window only moved, so did the field
                self._place_icon()
            return
        # A resize moves the field in ways only capture can tell: hide until it is still.
        self._anchor = anchor.moved(dx, dy)
        if self._panel_anchor is not None:
            self._panel_anchor = self._anchor
        if not self._resizing:
            self._resizing = True
            self._strip.hide()
            self._icon.hide()
            self._animate()
            if self._field_drawer():
                self._panel.hide()
        self._fresh = False
        for source in (self._settle, self._reshow):
            if source is not None:
                GLib.source_remove(source)
        self._reshow = None
        self._settle = GLib.timeout_add(SETTLE_MS, self._settled)

    def _settled(self) -> bool:
        self._settle = None
        self._reshow = GLib.timeout_add(max(1, RESHOW_MS - SETTLE_MS), self._reshow_now) \
            if not self._fresh else None
        if self._fresh:
            self._reshow_now()
        return GLib.SOURCE_REMOVE

    def _reshow_now(self) -> bool:
        self._reshow = None
        self._resizing = False
        self._strip_rect = self._panel_xy = None
        self._layout(force=True)
        self._place_icon()
        self._animate()
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------ "Rewrite as…"

    def ask_text(self, prompt: str, on_done: Callable[[str, bool], None]) -> None:
        """Ask for one line of text in a small window that takes the keyboard focus.

        It opens over the drawer's "Rewrite as…" field when the drawer is up. On Enter the
        window goes away, the window that was active before is activated again and
        `on_done(text, focus_returned)` is called once it is the active one - or, with
        `focus_returned` False, when it still is not after ASK_RETURN_MS. Escape (or the
        focus moving elsewhere) cancels: `on_done` is not called.
        """
        self._ask_close(reactivate=False)
        screen = self._screen()
        target = screen.get_active_window()
        win = Gtk.Window(title="Glance", resizable=False)
        win.set_keep_above(True)
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        win.set_decorated(False)
        win.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        _classes(win, "glance-root", "glance-window")
        box = _classes(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6),
                       "glance-drawer")
        box.pack_start(_label(prompt, "glance-meta"), False, False, 0)
        entry = Gtk.Entry(width_chars=44, max_length=300)
        entry.set_placeholder_text(ASK_PLACEHOLDER)
        box.pack_start(entry, False, False, 0)
        win.add(box)
        self._ask = {"win": win, "entry": entry, "target": target, "on_done": on_done}
        entry.connect("activate", lambda _e: self._ask_close(submit=True))
        win.connect("key-press-event", self._ask_key)
        win.connect("delete-event", lambda *_a: self._ask_close() or True)
        win.connect("focus-out-event", self._ask_focus_out)
        at = self._ask_position()
        if at is not None:
            win.move(*at)
        win.show_all()
        win.present_with_time(self._event_time(win))
        entry.grab_focus()

    def _ask_position(self) -> tuple[int, int] | None:
        field, xy = self._ask_input, self._panel_xy
        if field is not None and xy is not None and field.get_mapped():
            pos = field.translate_coordinates(self._panel, 0, 0)
            if pos is not None:
                return xy[0] + pos[0] - 17, xy[1] + pos[1] - 33
        return xy

    @staticmethod
    def _event_time(win: Gtk.Window) -> int:
        stamp = Gtk.get_current_event_time()
        if stamp:
            return stamp
        gi.require_version("GdkX11", "3.0")
        from gi.repository import GdkX11

        gdk_window = win.get_window()
        return GdkX11.x11_get_server_time(gdk_window) if gdk_window is not None else 0

    def _ask_key(self, _win: Gtk.Window, event: Gdk.EventKey) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            self._ask_close()
            return True
        return False

    def _ask_focus_out(self, _win: Gtk.Window, _event: Gdk.EventFocus) -> bool:
        # The user went somewhere else: that window keeps the focus, nothing is rewritten.
        self._ask_close(reactivate=False)
        return False

    def _ask_close(self, submit: bool = False, reactivate: bool = True) -> None:
        """Close the entry. Unless told not to, hand the focus back to where it was."""
        ask, self._ask = self._ask, None
        if self._ask_wait is not None:
            GLib.source_remove(self._ask_wait)
            self._ask_wait = None
        if ask is None:
            return
        win, target = ask["win"], ask["target"]
        text = ask["entry"].get_text() if submit else ""
        stamp = self._event_time(win)
        win.destroy()  # also ends its focus-out handler: `_ask` is already cleared
        if not reactivate:
            return
        if target is None:
            if submit:
                ask["on_done"](text, False)
            return
        target.activate(stamp)
        if not submit:
            return
        screen = self._screen()
        deadline = GLib.get_monotonic_time() + ASK_RETURN_MS * 1000

        def check() -> bool:
            active = screen.get_active_window()
            back = active is not None and active.get_xid() == target.get_xid()
            if not back and GLib.get_monotonic_time() < deadline:
                return GLib.SOURCE_CONTINUE
            self._ask_wait = None
            ask["on_done"](text, back)
            return GLib.SOURCE_REMOVE

        self._ask_wait = GLib.timeout_add(_ASK_POLL_MS, check)

    @property
    def ask_window(self) -> Gtk.Window | None:
        return None if self._ask is None else self._ask["win"]

    # ------------------------------------------------------------------ test access

    def status_geometry(self) -> tuple[int, int, int, int] | None:
        """(x, y, w, h) of the status icon's circle as placed, None while hidden."""
        if not self._icon.get_visible() or self._status_xy is None:
            return None
        return (self._status_xy[0] + ICON_X, self._status_xy[1] + ICON_Y, ICON, ICON)

    def status_state(self) -> dict[str, Any] | None:
        """What the icon shows (state, badge, animating), None while hidden."""
        v = self._status_view
        if v is None or not self._icon.get_visible():
            return None
        return {"state": v.state.value, "badge": v.badge, "spinning": self._spin is not None}

    def strip_geometry(self) -> tuple[int, int, int, int] | None:
        if not self._strip.get_visible() or self._strip_rect is None:
            return None
        return self._strip_rect

    def strip_text(self) -> str | None:
        """The corrected sentence (or note) on screen, None while the strip is hidden."""
        if not self._strip.get_visible() or self._strip_view is None:
            return None
        v = self._strip_view
        return v.text or v.guard or v.note

    def panel_geometry(self) -> tuple[int, int, int, int] | None:
        if not self._panel.get_visible() or self._panel_xy is None:
            return None
        return (*self._panel_xy, *self._panel_size)

    @property
    def status_window(self) -> Gtk.Window:
        return self._icon

    @property
    def strip_window(self) -> Gtk.Window:
        return self._strip

    @property
    def panel_window(self) -> Gtk.Window:
        return self._panel

    # ------------------------------------------------------------------ drawing

    def _accent(self) -> tuple[float, float, float]:
        return rgb(self._theme.tokens, "accent")

    def _restyle(self) -> None:
        """The desktop switched light/dark: the CSS is swapped; redraw what cairo draws."""
        self._icon.queue_draw()
        self._strip.queue_draw()
        if self._sel_view is not None:
            view, self._sel_view = self._sel_view, None
            self.show_selmark(view, *self._sel_xy)
        if self._strip_view is not None:
            self._build_strip(self._strip_view)
        if self._panel_view is not None:
            self._rebuild_panel(self._panel_view)  # its inline colours (the tint)
        if self._strip_view is not None or self._panel_view is not None:
            self._layout(force=True)

    def _draw_icon(self, widget: Gtk.Widget, cr: Any) -> bool:
        """The brand mark alone, no disc or ring (`look.icon_look`): softer and without its
        glow when idle, its glow breathing while working, full colour with a gentle glow and
        the count badge at its top right when ready, a grey silhouette when muted."""
        _clear(widget, cr)
        view = self._status_view
        if view is None:
            return True
        t = self._theme.tokens
        if not self._composited:  # cut to the mark: its soft edge on the theme's surface
            cr.set_source_rgb(*rgb(t, "surface"))
            cr.paint()
        lk = look.icon_look(view.state.value, view.spin, self._phase)
        cx, cy = ICON_X + ICON / 2, ICON_Y + ICON / 2
        x0, y0 = cx - MARK_PX / 2, cy - MARK_PX / 2
        if lk.glow > 0:
            paint_glow(cr, widget, x0, y0, MARK_PX, t[lk.glow_token], lk.glow)
        if lk.arc is not None:  # working: a thin arc turning around the mark
            cr.set_line_width(look.ARC_WIDTH)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.set_source_rgb(*rgb(t, "accent_fill"))
            cr.arc(cx, cy, look.ARC_RADIUS, lk.arc, lk.arc + look.ARC_SWEEP)
            cr.stroke()
        if lk.grey:
            paint_mark(cr, widget, x0, y0, MARK_PX, lk.mark_alpha, tint=t["dot_quiet"])
        else:
            paint_mark(cr, widget, x0, y0, MARK_PX, lk.mark_alpha)
        badge = view.badge
        if badge is not None:
            bx, by = BADGE_C
            dot = badge == "•"
            cr.arc(bx, by, _badge_r(badge), 0, 6.2832)
            cr.set_source_rgb(*rgb(t, "dot_warn" if dot else "accent_fill"))
            cr.fill()
            if not dot:
                cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
                cr.set_font_size(8.5 if len(badge) == 1 else 7)
                ext = cr.text_extents(badge)
                cr.move_to(bx - ext.width / 2 - ext.x_bearing,
                           by - ext.height / 2 - ext.y_bearing)
                cr.set_source_rgb(*rgb(t, "on_accent"))
                cr.show_text(badge)
        return True

    def _icon_pressed(self, _widget: Gtk.Widget, event: Gdk.EventButton) -> bool:
        # A quick second click also brings a 2BUTTON_PRESS: one toggle per click.
        if event.button == 1 and event.type == Gdk.EventType.BUTTON_PRESS:
            self._hide_menu()
            self._on_chip_click()
        return True

    # ------------------------------------------------------------------ the hover menu

    def _menu_enter(self, where: str) -> bool:
        if where == "icon":
            self._in_icon = True
        else:
            self._in_menu = True
        self._cancel_source("_menu_close_src")
        if where == "icon" and self._menu_view is None and self._menu_open_src is None:
            self._menu_open_src = GLib.timeout_add(MENU_OPEN_MS, self._menu_open_due)
        return False

    def _menu_left(self, win: Gtk.Window, event: Gdk.EventCrossing) -> bool:
        # Moving onto one of the menu's own buttons is a crossing too: not a leave.
        if event.detail != Gdk.NotifyType.INFERIOR:
            self._menu_leave("menu")
        return False

    def _menu_leave(self, where: str) -> bool:
        if where == "icon":
            self._in_icon = False
            self._cancel_source("_menu_open_src")
        else:
            self._in_menu = False
        if not (self._in_icon or self._in_menu) and self._menu_close_src is None:
            self._menu_close_src = GLib.timeout_add(MENU_CLOSE_MS, self._menu_close_due)
        return False

    def _cancel_source(self, attr: str) -> None:
        src = getattr(self, attr)
        if src is not None:
            GLib.source_remove(src)
            setattr(self, attr, None)

    def _menu_open_due(self) -> bool:
        self._menu_open_src = None
        if self._in_icon and self._status_xy is not None:
            menu = self._hover_menu()  # None while the user types
            if menu is not None:
                self._show_menu(menu)
        return GLib.SOURCE_REMOVE

    def _menu_close_due(self) -> bool:
        self._menu_close_src = None
        if not (self._in_icon or self._in_menu):
            self._hide_menu()
        return GLib.SOURCE_REMOVE

    def _show_menu(self, menu: HoverMenu) -> None:
        self._menu_view = menu
        old = self._menu.get_child()
        if old is not None:
            self._menu.remove(old)
            old.destroy()
        box = _classes(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1), "glance-menu")
        if menu.note:
            box.pack_start(_label(menu.note, "glance-note", chars=34), False, False, 0)
        for item in menu.items:
            button = self._button(item.label, item.action, "glance-item", **item.args)
            button.get_child().set_xalign(0)
            button.connect("clicked", lambda _b: self._hide_menu())
            box.pack_start(button, False, False, 0)
        if menu.hint:
            box.pack_start(self._hint(menu.hint), False, False, 0)
        fit = _Fit()
        fit.pack_start(box, True, True, 0)
        fit.show_all()
        self._menu.add(fit)
        x, y = self._status_xy or (0, 0)
        w, h = fit.get_preferred_size()[1].width, fit.get_preferred_size()[1].height
        mons = self.monitors()
        mx, my = clamp_to_monitor(x + ICON_BOX[0], y, (w, h), mons) if mons else (x, y)
        self._menu.resize(w, h)
        self._menu.move(mx, my)
        self._menu.show()
        if not self._composited:  # its rounded corners would be black: cut it to them
            gdk_window = self._menu.get_window()
            if gdk_window is not None:
                gdk_window.shape_combine_region(_rounded(0, 0, w, h, 10), 0, 0)
        _raise(self._menu)

    def _hide_menu(self) -> None:
        self._cancel_source("_menu_open_src")
        self._cancel_source("_menu_close_src")
        self._in_menu = False
        self._menu_view = None
        if self._menu.get_visible():
            self._menu.hide()

    def menu_items(self) -> list[str] | None:
        """Test access: the hover menu's item labels, None while it is closed."""
        v = self._menu_view
        return None if v is None or not self._menu.get_visible() else \
            [i.label for i in v.items]

    def _hint(self, hint: str) -> Gtk.Widget:
        """"Super+Alt+G to check" as keycaps and the muted rest."""
        chord, _sep, rest = hint.partition(" ")
        row = _classes(Gtk.Box(spacing=6), "glance-hint")
        row.pack_start(_keycaps(chord.split("+")), False, False, 0)
        if rest:
            row.pack_start(_label(rest, "glance-meta", wrap=False), False, False, 0)
        return row

    def _glyph(self, name: str, px: int = 14, token: str = "accent") -> Gtk.Widget:
        """A line glyph (`look.GLYPHS`) in the colour `token`."""
        area = Gtk.DrawingArea()
        area.set_size_request(px, px)
        area.set_valign(Gtk.Align.CENTER)

        def draw(_w: Gtk.Widget, cr: Any) -> bool:
            glyph_path(cr, name, 0.5, 0.5, px - 1)
            cr.set_source_rgb(*rgb(self._theme.tokens, token))
            cr.set_line_width(1.5 if px >= 14 else 1.3)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.set_line_join(cairo.LINE_JOIN_ROUND)
            cr.stroke()
            return True

        area.connect("draw", draw)
        return area

    # ------------------------------------------------------------------ the strip

    def _button(self, label: str, action: str, *classes: str, **args: Any) -> Gtk.Button:
        button = _pointer(Gtk.Button(label=label, can_focus=False, focus_on_click=False))
        _classes(button, *classes)
        child = button.get_child()
        if isinstance(child, Gtk.Label):
            child.set_ellipsize(Pango.EllipsizeMode.END)
            child.set_max_width_chars(40)
        button.connect("clicked", lambda _b: self._act(action, dict(args)))
        return button

    def _build_strip(self, view: StripView) -> None:
        old = self._strip.get_child()
        if old is not None:
            self._strip.remove(old)
            old.destroy()
        t = self._theme.tokens
        warn = view.guard is not None and not view.segments
        scroller: Gtk.Widget | None = None
        look_class = "glance-warn" if warn else "glance-plain" if not view.segments else None
        row = _classes(Gtk.Box(spacing=10), "glance-strip", *((look_class,) if look_class else ()))
        row.pack_start(_mark(16), False, False, 0)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.set_valign(Gtk.Align.CENTER)
        if view.segments:
            sentence = Gtk.Label(xalign=0, wrap=True, selectable=False)
            sentence.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            scroll = view.expanded and view.compact  # "⋯": the whole corrected text
            if not scroll:
                sentence.set_lines(5)
                sentence.set_ellipsize(Pango.EllipsizeMode.END)
            sentence.set_markup(look.strip_markup(view.shown, t["accent"]))
            _classes(sentence, "glance-sentence")
            if scroll:
                rect = self._strip_rect
                max_h = scroll_max_h(self.monitors(), *(rect[:2] if rect else (None, None)))
                scroller = self._scroller(sentence, max_h)
                text.pack_start(scroller, False, False, 0)
            else:
                text.pack_start(sentence, False, False, 0)
        if view.loading is not None:  # "Checking with Claude…": the AI's text is coming
            text.pack_start(_loader(view.loading, self._accent), False, False, 0)
        for line in (view.summary if view.segments else None, view.meta):
            if line is not None:
                text.pack_start(_label(line, "glance-note"), False, False, 0)
        if view.guard is not None:
            text.pack_start(_label(view.guard, "glance-guard"), False, False, 0)
        if view.note is not None and not view.segments:
            text.pack_start(_label(view.note, "glance-note"), False, False, 0)
        row.pack_start(text, True, True, 0)
        # Clicks go through the strip but on these; the scrolling text takes the wheel.
        self._strip_buttons = [] if scroller is None else [scroller]
        if view.can_use or (view.loading is not None and view.segments):
            chord = look.chord_text(self._use_key())
            use = self._button(chord, "use", "glance-use")
            use.set_valign(Gtk.Align.CENTER)
            row.pack_start(use, False, False, 0)
            if view.can_use:
                use.set_tooltip_text(f"Use this ({chord})")
                self._strip_buttons.append(use)
            else:  # the keycap waits for the AI's corrected text
                use.set_sensitive(False)
        if view.menu:
            more = self._button("⋯", "more", "glance-quiet",
                                *(("glance-on",) if view.expanded else ()))
            more.set_tooltip_text("Fewer options" if view.expanded else "More options")
            more.set_valign(Gtk.Align.CENTER)
            row.pack_start(more, False, False, 0)
            self._strip_buttons.append(more)
        fit = _Fit()
        fit.pack_start(row, True, True, 0)
        fit.show_all()
        self._strip.add(fit)
        self._strip_w = 0  # sized again for its width

    # ------------------------------------------------------------------ the drawer

    def _card(self, chip: ToneChip, loading: bool = False) -> Gtk.Button:
        """A tone chip: its glyph, its name, its number key and, once known, one line of
        its rewrite. A click selects it (emerald border and tint); nothing happens on hover
        but the hover look."""
        button = _classes(_pointer(Gtk.Button(can_focus=False, focus_on_click=False)),
                          "glance-card", *(("glance-selected",) if chip.selected else ()))
        button.set_name(f"card:{chip.key}")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        glyph = self._glyph(look.tone_glyph(chip.key), 15)
        glyph.set_halign(Gtk.Align.START)
        glyph.set_margin_bottom(3)
        box.pack_start(glyph, False, False, 0)
        title = _label(chip.label, "glance-card-title", wrap=False)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_width_chars(5)
        box.pack_start(title, False, False, 0)
        if look.card_skeleton(loading, chip.preview, chip.state):
            bar = _skeleton(int(90 * look.SKELETON_CARD[0]))
            bar.set_margin_top(4)
            box.pack_start(bar, False, False, 0)
        else:
            line = look.card_line(chip.preview, chip.state)
            preview = _label(line, "glance-card-preview", wrap=False, chars=18)
            preview.set_ellipsize(Pango.EllipsizeMode.END)
            preview.set_width_chars(8)
            box.pack_start(preview, False, False, 0)
        layers = Gtk.Overlay()
        layers.add(box)
        if chip.hotkey is not None:  # its number key, top right, on every card that has one
            kbd = _kbd(str(chip.hotkey))
            kbd.set_tooltip_text(f"Super+Alt+{chip.hotkey}")
            kbd.set_halign(Gtk.Align.END)
            kbd.set_valign(Gtk.Align.START)
            layers.add_overlay(kbd)
        button.add(layers)
        if chip.preview:
            button.set_tooltip_text(chip.preview)
        button.connect("clicked", lambda _b: self._act("tone", {"key": chip.key}))
        return button

    def _cards(self, chips: tuple[ToneChip, ...], loading: bool = False) -> Gtk.Widget:
        row = Gtk.Box(spacing=8, homogeneous=True)
        for chip in chips:
            row.pack_start(self._card(chip, loading), True, True, 0)
        return row

    def _rewrite_as(self) -> Gtk.Widget:
        """"Rewrite as… e.g. make it a bullet list" with the return key: a click opens the
        entry over it."""
        field = _classes(_pointer(Gtk.Button(can_focus=False, focus_on_click=False)),
                         "glance-input")
        field.set_name("rewrite-as")
        inner = Gtk.Box(spacing=8)
        inner.pack_start(self._glyph("pen", 14, "muted"), False, False, 0)
        inner.pack_start(_label(look.REWRITE_LEAD, "glance-input-lead", wrap=False),
                         False, False, 0)
        hint = _label(ASK_PLACEHOLDER, wrap=False)
        hint.set_ellipsize(Pango.EllipsizeMode.END)
        inner.pack_start(hint, True, True, 0)
        inner.pack_end(_kbd(look.RETURN_KEY), False, False, 0)
        field.add(inner)
        field.connect("clicked", lambda _b: self._act("tone", {"key": "custom"}))
        self._ask_input = field
        return field

    def _tones(self, view: PanelView) -> Gtk.Widget | None:
        """The "Tone" section: how the text reads, the chips, "Rewrite as…"."""
        cards = tuple(c for c in view.tone_chips if c.key != "custom")
        custom = any(c.key == "custom" for c in view.tone_chips)
        if not (cards or custom):
            return None
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        head = Gtk.Box(spacing=8)
        head.pack_start(_label(look.TONE, "glance-section", wrap=False), False, False, 0)
        line = look.tone_line(view.tone, view.tone_reason)
        if line:
            reads = _label(line, "glance-meta", wrap=False)
            reads.set_ellipsize(Pango.EllipsizeMode.END)
            head.pack_start(reads, True, True, 0)
        box.pack_start(head, False, False, 0)
        if cards:
            box.pack_start(self._cards(cards, view.loading), False, False, 0)
        if custom:
            box.pack_start(self._rewrite_as(), False, False, 0)
        return box

    def _suggested(self, s: SuggestedBox) -> Gtk.Widget:
        """"Suggested": the corrected text, or the selected tone's rewrite (its name beside
        the title, "Rewriting…" while it comes in), every changed word tinted, and
        "Apply (Alt+Enter)"."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        head = Gtk.Box(spacing=8)
        head.pack_start(_label(look.SUGGESTED, "glance-section", wrap=False), False, False, 0)
        if s.tone:
            head.pack_start(_label(s.tone, "glance-tone-tag", wrap=False), False, False, 0)
        if s.working:
            head.pack_end(_label(look.card_line(None, "loading"), "glance-meta", wrap=False),
                          False, False, 0)
        if s.loading is not None:
            head.pack_end(_loader(s.loading, self._accent), False, False, 0)
        box.pack_start(head, False, False, 0)
        if s.loading is not None:  # skeleton lines until the AI's corrected text is back
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=look.SKELETON_GAP)
            for share in look.SKELETON_SUGGESTED:
                text.pack_start(_skeleton(int((DRAWER_MIN_W - 64) * share)), False, False, 0)
        elif s.placeholder is not None:
            text = _label(s.placeholder, "glance-meta")
        else:
            text = Gtk.Label(xalign=0, wrap=True, selectable=False)
            text.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            text.set_markup(look.suggested_markup(s.segments, self._theme.tokens))
        frame = _classes(Gtk.Box(orientation=Gtk.Orientation.VERTICAL), "glance-suggest",
                         *(("glance-selected",) if s.tone else ()))
        frame.pack_start(self._scroller(text, _SUGGEST_MAX_H) if s.scroll else text,
                         False, False, 0)
        box.pack_start(frame, False, False, 0)
        if s.lines or s.can_apply:
            bottom = Gtk.Box(spacing=8)
            notes = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            notes.set_valign(Gtk.Align.CENTER)
            for line in s.lines:
                notes.pack_start(_label(line, "glance-meta"), False, False, 0)
            bottom.pack_start(notes, True, True, 0)
            if s.can_apply:
                apply = self._button(look.apply_label(self._use_key()), "use", "glance-primary")
                apply.set_valign(Gtk.Align.CENTER)
                bottom.pack_end(apply, False, False, 0)
            box.pack_start(bottom, False, False, 0)
        return box

    def _fix_line(self, view: PanelView) -> Gtk.Widget:
        """"3 fixes · its → it's · needs → need": a click applies that one fix."""
        line = Gtk.Box(spacing=2)
        n = len(view.issues)
        count = self._local_button(look.fixes_label(n), self._toggle_details, "glance-link")
        count.set_tooltip_text("Show each one, with more choices")
        line.pack_start(count, False, False, 0)
        shown = [r for r in view.issues if r.replacements][:_LINE_FIXES]
        for row in shown:
            line.pack_start(_label("·", "glance-meta", wrap=False), False, False, 0)
            flagged = (row.flagged or row.excerpt).replace("\n", " ")
            fix = self._button(f"{flagged} → {row.replacements[0] or '(remove)'}", "accept",
                               "glance-link", issue_id=row.issue_id, choice=0)
            fix.get_child().set_ellipsize(Pango.EllipsizeMode.NONE)
            fix.set_tooltip_text(row.message or None)
            line.pack_start(fix, False, False, 0)
        rest = n - len(shown)
        if rest > 0:
            line.pack_start(_label("·", "glance-meta", wrap=False), False, False, 0)
            line.pack_start(self._local_button(f"+{rest} more", self._toggle_details,
                                               "glance-link"), False, False, 0)
        if view.can_fix_all:
            line.pack_end(self._button(look.FIX_ALL, "fix_all", "glance-link",
                                       "glance-strong"), False, False, 0)
        return line

    def _local_button(self, label: str, fn: Callable[[], None], *classes: str) -> Gtk.Button:
        button = _pointer(Gtk.Button(label=label, can_focus=False, focus_on_click=False))
        _classes(button, *classes)
        button.connect("clicked", lambda _b: fn())
        return button

    def _toggle_details(self) -> None:
        self._details_open = not self._details_open
        if self._panel_view is not None:
            self._rebuild_panel(self._panel_view)
            self._layout(force=True)

    def _toggle_profiles(self) -> None:
        self._profiles_open = not self._profiles_open
        if self._panel_view is not None:
            self._rebuild_panel(self._panel_view)
            self._layout(force=True)

    def _issue_row(self, row: IssueRow) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.pack_start(_label(row.excerpt.replace("\n", " "), "glance-excerpt"),
                       False, False, 0)
        if row.message:
            box.pack_start(_label(row.message, "glance-meta"), False, False, 0)
        fixes = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=6,
                            row_spacing=6, max_children_per_line=6, homogeneous=False)
        for choice, replacement in enumerate(row.replacements[:_MAX_CHOICES]):
            fixes.add(self._button(replacement or "Remove", "accept", "glance-secondary",
                                   issue_id=row.issue_id, choice=choice))
        if row.rule == SPELL_RULE:
            fixes.add(self._button("Add to dictionary", "add_dict", "glance-link",
                                   issue_id=row.issue_id))
        fixes.add(self._button("Ignore here", "ignore", "glance-link", issue_id=row.issue_id,
                               scope="app"))
        fixes.add(self._button("Ignore everywhere", "ignore", "glance-link",
                               issue_id=row.issue_id, scope="global"))
        box.pack_start(fixes, False, False, 0)
        return box

    def _scroller(self, child: Gtk.Widget, max_h: int) -> Gtk.Widget:
        scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            propagate_natural_height=True, max_content_height=max_h, overlay_scrolling=False)
        scroller.add(child)
        return scroller

    def _guard_banner(self, text: str) -> Gtk.Widget:
        """The send-guard's question and the two ways to silence it."""
        box = _classes(Gtk.Box(spacing=8), "glance-banner")
        box.pack_start(_label(text), True, True, 0)
        box.pack_end(self._button("Never", "guard_silence", "glance-link", scope="global"),
                     False, False, 0)
        box.pack_end(self._button("Don't warn about this here", "guard_silence",
                                  "glance-link", scope="app"), False, False, 0)
        return box

    def _header(self, view: PanelView) -> Gtk.Widget:
        """The brand mark, "Glance Assistant" and what is at work; who it is for; Esc Close."""
        header = Gtk.Box(spacing=10)
        header.pack_start(_mark(22), False, False, 0)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        titles.set_valign(Gtk.Align.CENTER)
        has_tones = bool(view.tone_chips)
        titles.pack_start(_label(look.TITLE, "glance-title", wrap=False), False, False, 0)
        sub = look.subline(view.mode, has_tones, view.result and view.result.origin)
        titles.pack_start(_label(sub, "glance-meta", wrap=False), False, False, 0)
        line = None if has_tones else look.tone_line(view.tone, view.tone_reason)
        if line:  # no tone chips to put it over
            titles.pack_start(_label(line, "glance-meta"), False, False, 0)
        header.pack_start(titles, True, True, 0)
        close = _classes(_pointer(Gtk.Button(can_focus=False, focus_on_click=False)),
                         "glance-close")
        inner = Gtk.Box(spacing=6)
        inner.pack_start(_kbd(look.ESC_KEY), False, False, 0)
        inner.pack_start(_label(look.CLOSE, wrap=False), False, False, 0)
        close.add(inner)
        close.set_valign(Gtk.Align.CENTER)
        close.connect("clicked", lambda _b: self._act("close", {}))
        header.pack_end(close, False, False, 0)
        if view.profile_label:
            if view.mode == "selection":
                header.pack_end(_label(f"For {view.profile_label}", "glance-meta", wrap=False),
                                False, False, 0)
            else:
                audience = self._local_button(f"For {view.profile_label} ▾",
                                              self._toggle_profiles, "glance-link")
                audience.set_tooltip_text("Who this is written for")
                audience.set_valign(Gtk.Align.CENTER)
                header.pack_end(audience, False, False, 0)
        return header

    def _fill_drawer(self, card: Gtk.Box, view: PanelView) -> None:
        if view.guard:
            card.pack_start(self._guard_banner(view.guard), False, False, 0)
        card.pack_start(self._header(view), False, False, 0)
        if view.profile_label and view.profiles and self._profiles_open:
            menu = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=6,
                               row_spacing=6, max_children_per_line=5)
            for name, label in view.profiles:
                menu.add(self._button(label, "profile", "glance-secondary", name=name))
            card.pack_start(menu, False, False, 0)
        strip = self._strip_view  # shown here while the panel is open, not on its own
        box = suggested_box(view, strip)
        suggested = box is not None
        if box is not None:
            card.pack_start(self._suggested(box), False, False, 0)
        elif strip is not None and strip.note and not view.tone:
            card.pack_start(_label(strip.note, "glance-meta"), False, False, 0)
        tones = self._tones(view)
        if tones is not None:
            card.pack_start(tones, False, False, 0)
        if view.banner:
            card.pack_start(_classes(_label(view.banner), "glance-banner"), False, False, 0)
        if view.copy_offer is not None:
            rewrite = view.preview_state is not None
            offer = Gtk.Box(spacing=8)
            offer.pack_start(_label("The rewrite was not typed into the field." if rewrite
                                    else "The change could not be applied here.",
                                    "glance-meta"), True, True, 0)
            offer.pack_end(self._button("Copy rewrite" if rewrite else "Copy instead", "copy",
                                        "glance-secondary"), False, False, 0)
            card.pack_start(offer, False, False, 0)
        if view.issues and not view.loading:  # the AI's list replaces the quick one
            footer = _classes(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10),
                              "glance-footer")
            footer.pack_start(self._fix_line(view), False, False, 0)
            if self._details_open:
                rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
                for i, row in enumerate(view.issues):
                    item = self._issue_row(row)
                    if i:
                        _classes(item, "glance-row")
                    rows.pack_start(item, False, False, 0)
                footer.pack_start(self._scroller(rows, _LIST_MAX_H), False, False, 0)
            card.pack_start(footer, False, False, 0)
        elif not (view.banner or view.copy_offer is not None or view.tone_chips
                  or view.guard or suggested):
            card.pack_start(_label("Nothing to fix here.", "glance-meta"), False, False, 0)

    def _fill_selection(self, card: Gtk.Box, view: PanelView) -> None:
        """Select-anything: issues read-only, tone chips, a preview, and the two actions.
        "Copy rewrite" is the primary one; "Paste over selection" is hidden where
        `can_paste` is false (terminals)."""
        card.pack_start(self._header(view), False, False, 0)
        tones = self._tones(view)
        if tones is not None:
            card.pack_start(tones, False, False, 0)
        if view.preview is not None:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            box.pack_start(_label("Rewrite", "glance-section", wrap=False), False, False, 0)
            label = Gtk.Label(xalign=0, wrap=True, max_width_chars=52, selectable=False)
            label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            label.set_markup(diff_markup(view.preview))  # escaped there: the user's text
            box.pack_start(_classes(self._scroller(label, _PREVIEW_MAX_H), "glance-preview"),
                           False, False, 0)
            card.pack_start(box, False, False, 0)
        if view.preview_state == "loading":
            card.pack_start(_label("Rewriting…", "glance-meta"), False, False, 0)
        if view.banner:
            card.pack_start(_classes(_label(view.banner), "glance-banner"), False, False, 0)
        if view.issues:
            rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            for row in view.issues:
                item = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                item.pack_start(_label(row.excerpt.replace("\n", " "), "glance-excerpt"),
                                False, False, 0)
                if row.message:
                    item.pack_start(_label(row.message, "glance-meta"), False, False, 0)
                rows.pack_start(item, False, False, 0)
            card.pack_start(self._scroller(rows, _LIST_MAX_H), False, False, 0)
        footer = Gtk.Box(spacing=8)
        footer.pack_end(self._button("Copy rewrite", "sel_copy", "glance-primary"),
                        False, False, 0)
        if view.can_paste:
            footer.pack_end(self._button("Paste over selection", "sel_paste",
                                         "glance-secondary"), False, False, 0)
        card.pack_start(footer, False, False, 0)

    def _fill_result(self, card: Gtk.Box, view: PanelView) -> None:
        """Selection actions' answer: what was asked about (one muted line), the answer in
        a box (scrolling when long), then "Ask a follow-up…", "Replace selection" (only
        where a verified write-back is possible) and "Copy" (the default)."""
        r = view.result or ResultView("", "")
        card.pack_start(self._header(view), False, False, 0)
        head = Gtk.Box(spacing=8)
        head.pack_start(_label(r.title, "glance-tone-tag", wrap=False), False, False, 0)
        if r.state == "loading":  # the loader, as a requested check shows it
            head.pack_end(_loader(r.working, self._accent), False, False, 0)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.pack_start(head, False, False, 0)
        if r.source:
            box.pack_start(_label(f"“{r.source}”", "glance-meta"), False, False, 0)
        if r.text:
            text = Gtk.Label(label=r.text, xalign=0, wrap=True, selectable=False,
                             max_width_chars=58)
            text.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        elif r.state == "loading":  # skeleton lines until the answer starts to come in
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=look.SKELETON_GAP)
            for share in look.SKELETON_SUGGESTED:
                text.pack_start(_skeleton(int((SEL_W - 64) * share)), False, False, 0)
        else:
            text = _label("No answer this time.", "glance-meta")
        frame = _classes(Gtk.Box(orientation=Gtk.Orientation.VERTICAL), "glance-suggest")
        frame.pack_start(self._scroller(text, _SUGGEST_MAX_H)
                         if r.text and len(r.text) > 320 else text, False, False, 0)
        box.pack_start(frame, False, False, 0)
        card.pack_start(box, False, False, 0)
        if r.note:
            card.pack_start(_classes(_label(r.note), "glance-banner"), False, False, 0)
        if r.actions:  # text from a shot: Fix grammar, Translate, Explain
            more = Gtk.Box(spacing=8)
            for key, label in r.actions:
                more.pack_start(self._button(label, "res_action", "glance-secondary", key=key),
                                False, False, 0)
            card.pack_start(more, False, False, 0)
        footer = Gtk.Box(spacing=8)
        if r.can_follow and r.state != "loading":
            follow = self._button("Ask a follow-up…", "res_follow", "glance-link",
                                  "glance-strong")
            follow.set_valign(Gtk.Align.CENTER)
            footer.pack_start(follow, False, False, 0)
        if r.text and r.state == "ready":
            footer.pack_end(self._button("Copy", "res_copy", "glance-primary"), False, False, 0)
            if r.can_replace:
                footer.pack_end(self._button("Replace selection", "res_replace",
                                             "glance-secondary"), False, False, 0)
        card.pack_start(footer, False, False, 0)

    def _rebuild_panel(self, view: PanelView) -> None:
        self._ask_input = None
        old = self._panel.get_child()
        if old is not None:
            self._panel.remove(old)
            old.destroy()
        card = _classes(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14),
                        "glance-drawer")
        if view.mode == "selection":
            self._fill_selection(card, view)
        elif view.mode == "result":
            self._fill_result(card, view)
        else:
            self._fill_drawer(card, view)
        outer = _Fit()
        outer.set_margin_start(self._shadow[0])
        outer.set_margin_top(self._shadow[1])
        outer.set_margin_end(self._shadow[2])
        outer.set_margin_bottom(self._shadow[3])
        outer.pack_start(card, True, True, 0)
        outer.show_all()
        self._panel.add(outer)
        self._panel_w = 0  # sized again for its width
