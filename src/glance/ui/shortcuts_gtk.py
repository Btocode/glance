"""The "Keyboard shortcuts" window (the tray's item): Glance's chords as keycap rows. Main
thread only; it opens only when asked and takes nothing away from the app being written in
until then."""
from __future__ import annotations

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gtk

from glance.ui.overlay_gtk import _classes, _keycaps, _label, _mark
from glance.ui.theme import install

TITLE = "Keyboard shortcuts"
COLUMNS = 2


class ShortcutsWindow:
    """`show(rows)`: rows of (keycaps, what it does), from `shortcut_table.glance_shortcuts`."""

    def __init__(self) -> None:
        install()  # the same look as the other windows
        w = self.window = Gtk.Window(title=f"Glance — {TITLE}")
        w.set_resizable(False)
        w.set_border_width(0)
        w.set_position(Gtk.WindowPosition.CENTER)
        _classes(w, "glance-root", "glance-window")
        w.connect("delete-event", self._on_delete)
        w.connect("key-press-event", self._on_key)
        self._box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self._box.set_margin_start(22)
        self._box.set_margin_end(22)
        self._box.set_margin_top(18)
        self._box.set_margin_bottom(18)
        w.add(self._box)

    def show(self, rows: list[tuple[list[str], str]]) -> None:
        for child in self._box.get_children():
            self._box.remove(child)
            child.destroy()
        header = Gtk.Box(spacing=10)
        header.pack_start(_mark(22), False, False, 0)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        titles.pack_start(_label(TITLE, "glance-title", wrap=False), False, False, 0)
        titles.pack_start(_label("Glance works with the keyboard first.", "glance-meta",
                                 wrap=False), False, False, 0)
        header.pack_start(titles, True, True, 0)
        self._box.pack_start(header, False, False, 0)
        grid = Gtk.Grid(row_spacing=10, column_spacing=14)
        per = (len(rows) + COLUMNS - 1) // COLUMNS
        for i, (keys, what) in enumerate(rows):
            col, row = divmod(i, per)
            caps = _keycaps(keys, plus=True)
            caps.set_halign(Gtk.Align.START)
            grid.attach(caps, col * 2, row, 1, 1)
            label = _label(what, "glance-what", wrap=False)
            label.set_margin_end(18 if col < COLUMNS - 1 else 0)
            grid.attach(label, col * 2 + 1, row, 1, 1)
        self._box.pack_start(grid, False, False, 0)
        bar = Gtk.Box()
        close = _classes(Gtk.Button(label="Close"), "glance-secondary")
        close.connect("clicked", lambda _b: self.window.hide())
        bar.pack_end(close, False, False, 0)
        self._box.pack_start(bar, False, False, 0)
        self._box.show_all()
        self.window.show()
        self.window.present()

    def _on_key(self, _w: Gtk.Window, event: Gdk.EventKey) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            self.window.hide()
            return True
        return False

    def _on_delete(self, _w: Gtk.Window, _e: Gdk.Event) -> bool:
        self.window.hide()
        return True
