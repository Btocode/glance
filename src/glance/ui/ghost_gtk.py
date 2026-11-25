"""GtkGhost: the grey completion text after the caret (needs a live X session).

An override-redirect POPUP window with a transparent RGBA background, an empty input shape
(clicks go to the application underneath) and `accept_focus` off, so it can never take the
keyboard focus. The text is drawn at the font size the engine passes (the caret height times
0.81, findings S5.2), in the theme's muted green-grey (the strip's family, light or dark),
ellipsised at `max_width`, and the window is kept whole
on the monitor it is on. The text is only on screen, never stored beyond that.
"""
from __future__ import annotations

import cairo
import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, Gtk, Pango, PangoCairo

from glance.protocol import Rect
from glance.ui.placement import clamp_to_monitor
from glance.ui.theme import install, rgb

GHOST_FONT_FACTOR = 0.81  # S5.2: ghost font size as a share of the caret height
_FAMILY = "sans-serif"


class GtkGhost:
    """GhostPort. `show(text, x, y, ...)`: x is the caret's right edge, y its top."""

    def __init__(self, font_factor: float = GHOST_FONT_FACTOR) -> None:
        self._factor = font_factor
        self._theme = install()
        self._text = ""
        self._font_px = 0
        self._max_width = 1
        win = Gtk.Window(type=Gtk.WindowType.POPUP)
        win.set_accept_focus(False)
        win.set_focus_on_map(False)
        win.set_type_hint(Gdk.WindowTypeHint.TOOLTIP)
        win.set_keep_above(True)
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        win.set_resizable(False)
        visual = win.get_screen().get_rgba_visual()
        if visual is not None:
            win.set_visual(visual)
        win.set_app_paintable(True)
        win.connect("draw", self._draw)
        win.connect("realize", self._click_through)
        win.connect("map", self._click_through)
        self._win = win

    # ------------------------------------------------------------------ GhostPort

    def show(self, text: str, x: int, y: int, font_px: int, max_width: int) -> None:
        self._text, self._font_px, self._max_width = text, max(1, font_px), max(1, max_width)
        layout = self._layout()
        w, h = layout.get_pixel_size()
        size = (max(1, min(w, self._max_width)), max(1, h))
        # The caret height is not passed in: it is the font size over the factor. The line
        # box is centred in the caret rectangle, which is what lines the baselines up (S5.2).
        caret_h = round(self._font_px / self._factor)
        top = y + (caret_h - size[1]) // 2
        pos = clamp_to_monitor(x, top, size, self.monitors())
        self._win.set_size_request(*size)
        self._win.resize(*size)
        self._win.move(*pos)
        self._win.queue_draw()
        if not self._win.get_visible():
            self._win.show()

    def hide(self) -> None:
        if self._win.get_visible():
            self._win.hide()
        self._text = ""

    # ------------------------------------------------------------------ inspection (tests)

    @property
    def window(self) -> Gtk.Window:
        return self._win

    def shown_text(self) -> str | None:
        return self._text if self._win.get_visible() else None

    def font_px(self) -> int:
        return self._font_px

    def geometry(self) -> tuple[int, int, int, int]:
        return (*self._win.get_position(), *self._win.get_size())

    def monitors(self) -> list[Rect]:
        display = Gdk.Display.get_default()
        out = []
        for i in range(display.get_n_monitors()):
            g = display.get_monitor(i).get_geometry()
            out.append(Rect(g.x, g.y, g.width, g.height))
        return out

    # ------------------------------------------------------------------ drawing

    def _layout(self) -> Pango.Layout:
        layout = self._win.create_pango_layout(self._text)
        font = Pango.FontDescription()
        font.set_family(_FAMILY)
        font.set_absolute_size(self._font_px * Pango.SCALE)
        layout.set_font_description(font)
        layout.set_width(self._max_width * Pango.SCALE)
        layout.set_ellipsize(Pango.EllipsizeMode.END)
        layout.set_single_paragraph_mode(True)
        return layout

    def _click_through(self, *_args: object) -> None:
        gdk_window = self._win.get_window()
        if gdk_window is not None:
            gdk_window.input_shape_combine_region(cairo.Region(), 0, 0)

    def _draw(self, _widget: Gtk.Widget, cr: cairo.Context) -> bool:
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        cr.set_source_rgba(*rgb(self._theme.tokens, "ghost"), 1.0)
        layout = self._layout()
        cr.move_to(0, 0)
        PangoCairo.show_layout(cr, layout)
        return True
