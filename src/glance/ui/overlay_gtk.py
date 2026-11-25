"""GtkOverlay: the suggestion strip, its drawer and the status dot (needs a live X session).

All three are `Gtk.WindowType.POPUP` windows with `accept_focus` off: the window manager never
manages them, so they cannot take the keyboard focus from the app being written in (findings
S5.1). The strip lets clicks through everywhere but its two buttons. Everything is built from
the pure views; no text is kept here beyond what is on screen.

The strip and the drawer follow the window they belong to: when it moves they move with it
in the same frame (Wnck reports the new geometry); while it is being resized they hide, and
come back placed by the fresh field rectangles capture then sends, ~150 ms after it settles.

The one window that does take the focus is the "Rewrite as…" entry (`ask_text`): typing
needs it. It gives the focus back to the window that had it before it reports the answer.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import cairo
import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango

from glance.diffing import diff_markup
from glance.grammar.harper import SPELL_RULE
from glance.protocol import Rect
from glance.ui.chip_model import ChipState, ChipView, shows_dot
from glance.ui.panel_model import IssueRow, PanelView, ToneChip
from glance.ui.placement import Anchor, clamp_to_monitor, place_strip
from glance.ui.strip_model import StripView
from glance.ui.theme import install, rgb

DOT_PX = 8
DOT_BOX = 14  # the dot's window: a little room around it to aim at
DRAWER_MIN_W = 520
SEL_W = 460  # select-anything's panel
GAP = 4  # between the strip and the drawer
SHADOW = (10, 4, 10, 16)  # left, top, right, bottom room for the drawer's soft shadow
NO_SHADOW = (0, 0, 0, 0)  # without a compositor nothing is see-through: no room, no shadow
_LIST_MAX_H = 240
_MAX_CHOICES = 5
_LINE_FIXES = 3  # fixes listed on the drawer's one muted line; the rest behind "+N more"
_PREVIEW_MAX_H = 150
HOVER_MS = 350  # how long the pointer rests on a tone card before its preview is asked for
ASK_RETURN_MS = 1500  # how long the previous window gets to be the active one again
_ASK_POLL_MS = 20
SETTLE_MS = 150  # a resizing window is still for this long: show the strip again
RESHOW_MS = 400  # no fresh field rectangles by then: show it where it was, moved along
ASK_PLACEHOLDER = "e.g. make it a bullet list"
# The sparkle of the mockup, in a 24 px box: M12 3 l1.8 4.7 L18.5 9.5 ... z
_SPARKLE = ((12, 3), (13.8, 7.7), (18.5, 9.5), (13.8, 11.3), (12, 16), (10.2, 11.3),
            (5.5, 9.5), (10.2, 7.7))
_DOT_TOKEN = {ChipState.PAUSED: "dot_quiet", ChipState.PRIVATE: "dot_quiet",
              ChipState.AI_LIMIT: "dot_warn", ChipState.NO_AI: "dot_info"}


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


def _raise(win: Gtk.Window) -> None:
    """On top of the windows mapped since it was made (an override-redirect window keeps
    its place in the stack when it is mapped again). Raising never moves the focus."""
    gdk_window = win.get_window()
    if gdk_window is not None:
        gdk_window.raise_()


def _clear(_widget: Gtk.Widget, cr: Any) -> bool:
    cr.set_source_rgba(0, 0, 0, 0)
    cr.set_operator(cairo.OPERATOR_SOURCE)
    cr.paint()
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


def _escape(text: str) -> str:
    return GLib.markup_escape_text(text, -1)


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
    tone_apply{key} (a click on a tone card), tone_hover{key} (the pointer rested on one),
    tone{key:"custom"} ("Rewrite as…"), profile{name}, copy, close and guard_silence{scope}
    (the send-guard banner's buttons); in a select-anything panel (`mode == "selection"`)
    tone{key}, tone_hover{key}, sel_copy, sel_paste and close."""

    def __init__(self, on_chip_click: Callable[[], None],
                 on_panel_action: Callable[[str, dict[str, Any]], None]) -> None:
        self._on_chip_click = on_chip_click
        self._act = on_panel_action
        self._theme = install()
        self._theme.on_change(self._restyle)
        screen = Gdk.Screen.get_default()
        self._composited = bool(screen is not None and screen.is_composited())
        self._shadow = SHADOW if self._composited else NO_SHADOW
        self._chip_view: ChipView | None = None
        self._chip_xy: tuple[int, int] | None = None
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
        self._hover: int | None = None  # GLib source of the pending tone hover
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

        self._chip = _popup(Gdk.WindowTypeHint.TOOLTIP)
        self._chip.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self._chip.connect("draw", self._draw_chip)
        self._chip.connect("button-press-event", self._chip_pressed)
        self._chip.set_size_request(DOT_BOX, DOT_BOX)

        self._strip = _popup(Gdk.WindowTypeHint.TOOLTIP)
        self._strip.connect("draw", _clear)
        self._strip.connect("size-allocate", lambda *_a: self._strip_shape())
        self._strip.connect("map", lambda *_a: self._strip_shape())

        self._panel = _popup(Gdk.WindowTypeHint.POPUP_MENU)
        self._panel.connect("draw", _clear)
        self._panel.connect("size-allocate", lambda *_a: self._panel_shape())
        self._panel.connect("map", lambda *_a: self._panel_shape())

        screen = Gdk.Screen.get_default()
        if screen is not None:
            screen.connect("monitors-changed", lambda *_a: self._layout())
        try:
            wnck = self._screen()
            wnck.connect("active-window-changed", lambda *_a: self._follow_active())
            self._follow_active()
        except Exception:  # noqa: BLE001 - no Wnck: the strip still follows each snapshot
            self._wnck_screen = None

    # ------------------------------------------------------------------ OverlayPort

    def show_chip(self, view: ChipView, x: int, y: int) -> None:
        if not shows_dot(view):  # the strip speaks for every other state
            self.hide_chip()
            return
        if view != self._chip_view:
            self._chip_view = view
            self._chip.set_tooltip_text(view.label or None)
            self._chip.queue_draw()
        xy = clamp_to_monitor(x, y, (DOT_BOX, DOT_BOX), self.monitors())
        if xy != self._chip_xy or not self._chip.get_visible():
            self._chip_xy = xy
            self._chip.move(*xy)
        if not self._chip.get_visible():
            self._chip.show()
            _raise(self._chip)

    def hide_chip(self) -> None:
        if self._chip.get_visible():
            self._chip.hide()
        self._chip_xy = None
        self._chip_view = None

    def show_strip(self, view: StripView, anchor: Anchor) -> None:
        self._fresh = True
        rebuilt = view != self._strip_view
        if rebuilt:
            self._strip_view = view
            self._build_strip(view)
        self._anchor = anchor
        self._layout(force=rebuilt)

    def hide_strip(self) -> None:
        had = self._strip_view is not None
        self._strip_view = None
        self._strip_rect = None
        if self._strip.get_visible():
            self._strip.hide()
        if had and self._panel_anchor is not None:
            self._layout()  # the drawer moves up into the strip's place

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
        self._cancel_hover()
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

    # ------------------------------------------------------------------ placing

    def _field_drawer(self) -> bool:
        return self._panel_view is not None and self._panel_anchor is not None

    def _layout(self, force: bool = False) -> None:
        """Put the strip and the field's drawer where the anchor says, one above the other.

        The drawer goes under the strip; when the two do not fit below the line they go
        above it, the strip nearest the line. Nothing is shown while the window resizes.
        """
        mons = self.monitors()
        if self._panel_view is not None and self._panel_anchor is None:
            self._place_selection(mons)
        strip, drawer = self._strip_view is not None, self._field_drawer()
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
            return
        # A resize moves the field in ways only capture can tell: hide until it is still.
        self._anchor = anchor.moved(dx, dy)
        if self._panel_anchor is not None:
            self._panel_anchor = self._anchor
        if not self._resizing:
            self._resizing = True
            self._strip.hide()
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

    def chip_geometry(self) -> tuple[int, int, int, int] | None:
        """(x, y, w, h) of the status dot's window as placed, None while hidden."""
        if not self._chip.get_visible() or self._chip_xy is None:
            return None
        return (*self._chip_xy, DOT_BOX, DOT_BOX)

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
    def chip_window(self) -> Gtk.Window:
        return self._chip

    @property
    def strip_window(self) -> Gtk.Window:
        return self._strip

    @property
    def panel_window(self) -> Gtk.Window:
        return self._panel

    # ------------------------------------------------------------------ drawing

    def _restyle(self) -> None:
        """The desktop switched light/dark: the CSS is swapped; redraw what cairo draws."""
        self._chip.queue_draw()
        self._strip.queue_draw()
        if self._strip_view is not None:
            self._build_strip(self._strip_view)
            self._layout(force=True)

    def _draw_chip(self, widget: Gtk.Widget, cr: Any) -> bool:
        _clear(widget, cr)
        view = self._chip_view
        if view is None:
            return True
        t = self._theme.tokens
        cr.arc(DOT_BOX / 2, DOT_BOX / 2, DOT_PX / 2 + 1, 0, 6.2832)
        cr.set_source_rgba(*rgb(t, "surface"), 0.9)
        cr.fill()
        cr.arc(DOT_BOX / 2, DOT_BOX / 2, DOT_PX / 2, 0, 6.2832)
        cr.set_source_rgb(*rgb(t, _DOT_TOKEN.get(view.state, "dot_quiet")))
        cr.fill()
        return True

    def _chip_pressed(self, _widget: Gtk.Widget, event: Gdk.EventButton) -> bool:
        if event.button == 1:
            self._on_chip_click()
        return True

    def _sparkle(self, warn: bool) -> Gtk.Widget:
        area = Gtk.DrawingArea()
        area.set_size_request(16, 16)
        area.set_valign(Gtk.Align.CENTER)

        def draw(_w: Gtk.Widget, cr: Any) -> bool:
            t = self._theme.tokens
            cr.scale(16 / 24, 16 / 24)
            cr.move_to(*_SPARKLE[0])
            for point in _SPARKLE[1:]:
                cr.line_to(*point)
            cr.close_path()
            cr.set_source_rgb(*rgb(t, "warn_text" if warn else "accent"))
            cr.set_line_width(2)
            cr.set_line_join(cairo.LINE_JOIN_ROUND)
            cr.stroke()
            return True

        area.connect("draw", draw)
        return area

    # ------------------------------------------------------------------ the strip

    def _button(self, label: str, action: str, *classes: str, **args: Any) -> Gtk.Button:
        button = Gtk.Button(label=label, can_focus=False, focus_on_click=False)
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
        row = _classes(Gtk.Box(spacing=10), "glance-strip", *(("glance-warn",) if warn else ()))
        row.pack_start(self._sparkle(warn), False, False, 0)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.set_valign(Gtk.Align.CENTER)
        if view.segments:
            sentence = Gtk.Label(xalign=0, wrap=True, selectable=False)
            sentence.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            sentence.set_lines(3)
            sentence.set_ellipsize(Pango.EllipsizeMode.END)
            sentence.set_markup("".join(
                f'<span foreground="{t["accent"]}" weight="600">{_escape(s.text)}</span>'
                if s.changed else _escape(s.text) for s in view.segments))
            text.pack_start(_classes(sentence, "glance-sentence"), False, False, 0)
        if view.guard is not None:
            text.pack_start(_label(view.guard, "glance-guard"), False, False, 0)
        if view.note is not None and not view.segments:
            text.pack_start(_label(view.note, "glance-note"), False, False, 0)
        row.pack_start(text, True, True, 0)
        self._strip_buttons = []
        if view.can_use:
            use = self._button("Use  Alt+⏎", "use", "glance-primary")
            use.set_tooltip_text("Replace with this (Alt+Enter)")
            use.set_valign(Gtk.Align.CENTER)
            row.pack_start(use, False, False, 0)
            self._strip_buttons.append(use)
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

    def _cancel_hover(self) -> None:
        if self._hover is not None:
            GLib.source_remove(self._hover)
            self._hover = None

    def _hovered(self, button: Gtk.Button, key: str) -> None:
        def fire() -> bool:
            self._hover = None
            self._act("tone_hover", {"key": key})
            return GLib.SOURCE_REMOVE

        def enter(_b: Gtk.Widget, _e: Gdk.EventCrossing) -> bool:
            self._cancel_hover()
            self._hover = GLib.timeout_add(HOVER_MS, fire)
            return False

        def leave(_b: Gtk.Widget, _e: Gdk.EventCrossing) -> bool:
            self._cancel_hover()
            return False

        button.connect("enter-notify-event", enter)
        button.connect("leave-notify-event", leave)

    def _card(self, chip: ToneChip, action: str) -> Gtk.Button:
        """A tone: its name and, once known, one line of its rewrite."""
        button = _classes(Gtk.Button(can_focus=False, focus_on_click=False), "glance-card")
        button.set_name(f"card:{chip.key}")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        top = Gtk.Box(spacing=6)
        title = _label(chip.label, "glance-card-title", wrap=False)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_width_chars(6)
        top.pack_start(title, True, True, 0)
        if chip.hotkey is not None:  # the same small key hint on every card that has one
            kbd = _label(str(chip.hotkey), "glance-kbd", wrap=False)
            kbd.set_tooltip_text(f"Super+Alt+{chip.hotkey}")
            kbd.set_valign(Gtk.Align.CENTER)
            top.pack_end(kbd, False, False, 0)
        box.pack_start(top, False, False, 0)
        if chip.state == "loading" and not chip.preview:
            line = "Rewriting…"
        elif chip.state == "failed":
            line = "Did not work"
        elif chip.preview:
            line = " ".join(chip.preview.split())
        else:
            line = "Hover to preview"
        preview = _label(line, "glance-card-preview", wrap=False, chars=18)
        preview.set_ellipsize(Pango.EllipsizeMode.END)
        preview.set_width_chars(8)
        box.pack_start(preview, False, False, 0)
        button.add(box)
        if chip.preview:
            button.set_tooltip_text(chip.preview)
        button.connect("clicked", lambda _b: self._act(action, {"key": chip.key}))
        self._hovered(button, chip.key)
        return button

    def _cards(self, chips: tuple[ToneChip, ...], action: str) -> Gtk.Widget:
        row = Gtk.Box(spacing=8, homogeneous=True)
        for chip in chips:
            row.pack_start(self._card(chip, action), True, True, 0)
        return row

    def _rewrite_as(self) -> Gtk.Widget:
        row = Gtk.Box(spacing=8)
        row.pack_start(_label("Rewrite as…", "glance-meta", wrap=False), False, False, 0)
        field = _classes(Gtk.Button(can_focus=False, focus_on_click=False), "glance-input")
        field.set_name("rewrite-as")
        field.add(_label(ASK_PLACEHOLDER, wrap=False))
        field.connect("clicked", lambda _b: self._act("tone", {"key": "custom"}))
        row.pack_start(field, True, True, 0)
        self._ask_input = field
        return row

    def _fix_line(self, view: PanelView) -> Gtk.Widget:
        """"3 fixes · its → it's · needs → need": a click applies that one fix."""
        line = Gtk.Box(spacing=2)
        n = len(view.issues)
        count = self._local_button(f"{n} fix{'' if n == 1 else 'es'}", self._toggle_details,
                                   "glance-link")
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
            line.pack_end(self._button("Fix all", "fix_all", "glance-link", "glance-strong"),
                          False, False, 0)
        return line

    def _local_button(self, label: str, fn: Callable[[], None], *classes: str) -> Gtk.Button:
        button = Gtk.Button(label=label, can_focus=False, focus_on_click=False)
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

    def _header(self, view: PanelView, title: str | None) -> Gtk.Widget:
        header = Gtk.Box(spacing=6)
        if title:
            header.pack_start(_label(title, "glance-title", wrap=False), False, False, 0)
        tone = None
        if view.tone:
            tone = f"Reads {view.tone.lower()}"
            if view.tone_reason:
                tone += f" — {view.tone_reason}"
        if tone:
            header.pack_start(_label(tone, "glance-meta"), True, True, 0)
        header.pack_end(self._button("Close", "close", "glance-link"), False, False, 0)
        if view.profile_label:
            if view.mode == "selection":
                header.pack_end(_label(f"For {view.profile_label}", "glance-meta", wrap=False),
                                False, False, 0)
            else:
                audience = self._local_button(f"For {view.profile_label} ▾",
                                              self._toggle_profiles, "glance-link")
                audience.set_tooltip_text("Who this is written for")
                header.pack_end(audience, False, False, 0)
        return header

    def _fill_drawer(self, card: Gtk.Box, view: PanelView) -> None:
        if view.guard:
            card.pack_start(self._guard_banner(view.guard), False, False, 0)
        card.pack_start(self._header(view, None), False, False, 0)
        if view.profile_label and view.profiles and self._profiles_open:
            menu = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=6,
                               row_spacing=6, max_children_per_line=5)
            for name, label in view.profiles:
                menu.add(self._button(label, "profile", "glance-secondary", name=name))
            card.pack_start(menu, False, False, 0)
        cards = tuple(c for c in view.tone_chips if c.key != "custom")
        if cards:
            card.pack_start(self._cards(cards, "tone_apply"), False, False, 0)
        if any(c.key == "custom" for c in view.tone_chips):
            card.pack_start(self._rewrite_as(), False, False, 0)
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
        if view.issues:
            card.pack_start(self._fix_line(view), False, False, 0)
            if self._details_open:
                rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
                for i, row in enumerate(view.issues):
                    item = self._issue_row(row)
                    if i:
                        _classes(item, "glance-row")
                    rows.pack_start(item, False, False, 0)
                card.pack_start(self._scroller(rows, _LIST_MAX_H), False, False, 0)
        elif not (view.banner or view.copy_offer is not None or view.tone_chips
                  or view.guard):
            card.pack_start(_label("Nothing to fix here.", "glance-meta"), False, False, 0)

    def _fill_selection(self, card: Gtk.Box, view: PanelView) -> None:
        """Select-anything: issues read-only, tone cards, a preview, and the two actions.
        "Copy rewrite" is the primary one; "Paste over selection" is hidden where
        `can_paste` is false (terminals)."""
        card.pack_start(self._header(view, "Selected text"), False, False, 0)
        cards = tuple(c for c in view.tone_chips if c.key != "custom")
        if cards:
            card.pack_start(self._cards(cards, "tone"), False, False, 0)
        if any(c.key == "custom" for c in view.tone_chips):
            card.pack_start(self._rewrite_as(), False, False, 0)
        if view.preview is not None:
            label = Gtk.Label(xalign=0, wrap=True, max_width_chars=52, selectable=False)
            label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            label.set_markup(diff_markup(view.preview))  # escaped there: the user's text
            card.pack_start(_classes(self._scroller(label, _PREVIEW_MAX_H), "glance-preview"),
                            False, False, 0)
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

    def _rebuild_panel(self, view: PanelView) -> None:
        self._cancel_hover()
        self._ask_input = None
        old = self._panel.get_child()
        if old is not None:
            self._panel.remove(old)
            old.destroy()
        card = _classes(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10),
                        "glance-drawer")
        if view.mode == "selection":
            self._fill_selection(card, view)
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
