"""A small read-only window with the text of `glance usage`. Main thread only."""
from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gtk  # noqa: E402

from glance.ui.theme import install  # noqa: E402


class UsageWindow:
    def __init__(self) -> None:
        install()  # the same look as the other windows
        w = self.window = Gtk.Window(title="AI usage")
        w.set_default_size(720, 420)
        w.set_border_width(16)
        w.get_style_context().add_class("glance-root")
        w.get_style_context().add_class("glance-window")
        w.connect("delete-event", self._on_delete)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        w.add(box)
        scroll = Gtk.ScrolledWindow()
        self._view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                  wrap_mode=Gtk.WrapMode.WORD_CHAR)
        scroll.add(self._view)
        box.pack_start(scroll, True, True, 0)
        close = Gtk.Button(label="Close")
        close.get_style_context().add_class("glance-secondary")
        close.connect("clicked", lambda _b: self.window.hide())
        bar = Gtk.Box()
        bar.pack_end(close, False, False, 0)
        box.pack_start(bar, False, False, 0)
        box.show_all()

    def show(self, text: str) -> None:
        self._view.get_buffer().set_text(text)
        self.window.show()
        self.window.present()

    def _on_delete(self, _w: Gtk.Window, _e: Gdk.Event) -> bool:
        self.window.hide()
        return True
