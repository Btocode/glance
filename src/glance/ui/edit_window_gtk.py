"""The Ctrl+G window: a normal, focused GTK window over the terminal running Claude Code.

Unlike the overlay this window is meant to take the keyboard: the user asked for it, and the
terminal behind it waits for `glance-edit` anyway. It is Glance's own window, so issues are
underlined right in its text view.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango

from glance.diffing import diff_markup, word_diff
from glance.features.edit_session import (
    EditSession,
    EditSessionHost,
    Preview,
    RunBg,
)
from glance.protocol import Rect
from glance.ui.placement import clamp_to_monitor

TITLE = "Glance — edit prompt"
SIZE = (640, 560)
ISSUE_REFRESH_MS = 300
_MAX_CHOICES = 5
_NOT_APPLICABLE = {
    "no_change": "No change suggested.",
    "placeholders": "The rewrite lost a protected part: copy it instead.",
    "objects": "The prompt holds embedded objects: copy the rewrite instead.",
    "rich": "Typing this back would lose formatting: copy it instead.",
    "limit": "Today's AI limit is reached.",
    "none": "No AI backend is set up.",
    "battery": "AI is off on battery.",
    "failed": "The rewrite failed.",
    "cancelled": "The rewrite was cancelled.",
}
_CSS = b"""
.glance-edit .glance-dim { color: #6a6a6e; font-size: smaller; }
.glance-edit .glance-preview { background-color: #ffffff; border: 1px solid #d6d6d2;
                               border-radius: 4px; padding: 6px; color: #1d1d1f; }
"""


def monitors() -> list[Rect]:
    display = Gdk.Display.get_default()
    out = []
    for i in range(display.get_n_monitors()):
        g = display.get_monitor(i).get_geometry()
        out.append(Rect(g.x, g.y, g.width, g.height))
    return out


def centre_over(over: Rect | None, size: tuple[int, int], mons: list[Rect]) -> tuple[int, int]:
    """Top-left of a `size` window centred over `over` (or the first monitor), kept whole on
    the monitor under it."""
    w, h = size
    target = over or (mons[0] if mons else Rect(0, 0, w, h))
    x, y = target.x + (target.w - w) // 2, target.y + (target.h - h) // 2
    return clamp_to_monitor(x, y, size, mons)


def window_rect(xid: int) -> Rect | None:
    """Frame rectangle of X window `xid` in root coordinates; xid 0 means the active window
    (the terminal, at the moment Ctrl+G is pressed). None when it cannot be found."""
    gi.require_version("GdkX11", "3.0")
    from gi.repository import GdkX11

    display = Gdk.Display.get_default()
    if xid:
        win = GdkX11.X11Window.foreign_new_for_display(display, xid)
    else:
        win = Gdk.Screen.get_default().get_active_window()
    if win is None:
        return None
    r = win.get_frame_extents()
    return Rect(r.x, r.y, r.width, r.height) if r.width > 1 and r.height > 1 else None


class EditWindow:
    """`on_done(action, text)` fires once: ("apply", text), ("editor", text) or
    ("cancel", "")."""

    def __init__(self, session: EditSession, over: Rect | None,
                 on_done: Callable[[str, str], None]) -> None:
        self._s = session
        self._on_done = on_done
        self._done = False
        self._loading = False
        self._refresh_src: int | None = None

        css = Gtk.CssProvider()
        css.load_from_data(_CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        win = self.window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL, title=TITLE)
        win.get_style_context().add_class("glance-edit")
        win.set_type_hint(Gdk.WindowTypeHint.NORMAL)
        win.set_keep_above(True)
        win.set_default_size(*SIZE)
        win.move(*centre_over(over, SIZE, monitors()))
        win.connect("delete-event", self._on_delete)
        win.connect("key-press-event", self._on_key)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=12)
        win.add(box)
        head = Gtk.Label(label="Prompt for Claude Code", xalign=0)
        head.get_style_context().add_class("glance-dim")
        box.pack_start(head, False, False, 0)

        self.buffer = Gtk.TextBuffer()
        self._issue_tag = self.buffer.create_tag("issue", underline=Pango.Underline.ERROR)
        self.view = Gtk.TextView(buffer=self.buffer, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                 left_margin=6, right_margin=6, top_margin=6, bottom_margin=6)
        scroll = Gtk.ScrolledWindow(min_content_height=150)
        scroll.set_shadow_type(Gtk.ShadowType.IN)
        scroll.add(self.view)
        box.pack_start(scroll, True, True, 0)
        self.buffer.connect("changed", self._on_changed)

        chips = Gtk.Box(spacing=6)
        self.chip_buttons: dict[str, Gtk.Button] = {}
        for key, label in session.chips:
            b = Gtk.Button(label=label)
            b.connect("clicked", lambda _b, k=key: self._chip(k))
            chips.pack_start(b, False, False, 0)
            self.chip_buttons[key] = b
        box.pack_start(chips, False, False, 0)

        self.custom_row = Gtk.Box(spacing=6, no_show_all=True)
        self.custom_entry = Gtk.Entry(placeholder_text="Rewrite as… (e.g. step by step)")
        self.custom_entry.connect("activate", lambda _e: self._run_custom())
        go = Gtk.Button(label="Rewrite")
        go.connect("clicked", lambda _b: self._run_custom())
        self.custom_row.pack_start(self.custom_entry, True, True, 0)
        self.custom_row.pack_start(go, False, False, 0)
        self.custom_entry.show()
        go.show()
        box.pack_start(self.custom_row, False, False, 0)

        self.preview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                                   no_show_all=True)
        self.preview_label = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.preview_label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.preview_label.get_style_context().add_class("glance-preview")
        self.preview_status = Gtk.Label(xalign=0)
        self.preview_status.get_style_context().add_class("glance-dim")
        prow = Gtk.Box(spacing=6)
        self.use_button = Gtk.Button(label="Use rewrite")
        self.use_button.connect("clicked", lambda _b: self._use_preview())
        self.copy_button = Gtk.Button(label="Copy rewrite")
        self.copy_button.connect("clicked", lambda _b: self._copy_preview())
        prow.pack_start(self.use_button, False, False, 0)
        prow.pack_start(self.copy_button, False, False, 0)
        prow.pack_start(self.preview_status, True, True, 0)
        pscroll = Gtk.ScrolledWindow(min_content_height=60, max_content_height=150,
                                     propagate_natural_height=True)
        pscroll.add(self.preview_label)
        for w in (pscroll, prow):
            w.show_all()
            self.preview_box.pack_start(w, False, False, 0)
        box.pack_start(self.preview_box, False, False, 0)

        irow = Gtk.Box(spacing=6)
        self.issues_label = Gtk.Label(xalign=0)
        self.fix_all_button = Gtk.Button(label="Fix all")
        self.fix_all_button.connect("clicked", lambda _b: self._mutate(self._s.fix_all_safe))
        self.undo_button = Gtk.Button(label="Undo")
        self.undo_button.connect("clicked", lambda _b: self._mutate(self._s.undo))
        irow.pack_start(self.issues_label, True, True, 0)
        irow.pack_start(self.fix_all_button, False, False, 0)
        irow.pack_start(self.undo_button, False, False, 0)
        box.pack_start(irow, False, False, 0)
        self.issue_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        iscroll = Gtk.ScrolledWindow(min_content_height=60, max_content_height=140,
                                     propagate_natural_height=True)
        iscroll.add(self.issue_list)
        box.pack_start(iscroll, False, False, 0)

        buttons = Gtk.Box(spacing=6)
        cancel = Gtk.Button(label="Cancel")
        cancel.set_tooltip_text("Esc: leave the prompt as it was")
        cancel.connect("clicked", lambda _b: self._finish("cancel"))
        editor = Gtk.Button(label="Open in my editor")
        editor.set_tooltip_text("Ctrl+E")
        editor.connect("clicked", lambda _b: self._finish("editor"))
        apply = Gtk.Button(label="Apply")
        apply.set_tooltip_text("Ctrl+Enter")
        apply.get_style_context().add_class("suggested-action")
        apply.connect("clicked", lambda _b: self._finish("apply"))
        buttons.pack_end(apply, False, False, 0)
        buttons.pack_end(editor, False, False, 0)
        buttons.pack_end(cancel, False, False, 0)
        box.pack_start(buttons, False, False, 0)

        self._load()
        win.show_all()
        self.present()
        self.view.grab_focus()
        self.buffer.place_cursor(self.buffer.get_end_iter())

    # -- window ----------------------------------------------------------------
    def present(self) -> None:
        gdk_win = self.window.get_window()
        try:
            gi.require_version("GdkX11", "3.0")
            from gi.repository import GdkX11

            self.window.present_with_time(GdkX11.x11_get_server_time(gdk_win))
        except (ValueError, TypeError, AttributeError):
            self.window.present()

    def _finish(self, action: str) -> None:
        if self._done:
            return
        self._done = True
        text = self.text() if action in ("apply", "editor") else ""
        if self._refresh_src is not None:
            GLib.source_remove(self._refresh_src)
            self._refresh_src = None
        self.window.destroy()
        self._on_done(action, text)

    def _on_delete(self, *_a: Any) -> bool:
        self._finish("cancel")
        return True

    def _on_key(self, _w: Any, event: Any) -> bool:
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        key = event.keyval
        if key == Gdk.KEY_Escape:
            self._finish("cancel")
            return True
        if ctrl and key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._finish("apply")
            return True
        if ctrl and key in (Gdk.KEY_e, Gdk.KEY_E):
            self._finish("editor")
            return True
        return False

    # -- text and issues -------------------------------------------------------
    def text(self) -> str:
        b = self.buffer
        return b.get_text(b.get_start_iter(), b.get_end_iter(), True)

    def _load(self) -> None:
        """Show the session's text (after an accept, a rewrite or an undo)."""
        if self.text() != self._s.text:
            self._loading = True
            try:
                self.buffer.set_text(self._s.text)
            finally:
                self._loading = False
        self._show_issues()
        self._show_preview(None)

    def _on_changed(self, _b: Any) -> None:
        if self._loading:
            return
        self._s.set_text(self.text())
        self._show_preview(None)
        if self._refresh_src is not None:
            GLib.source_remove(self._refresh_src)
        self._refresh_src = GLib.timeout_add(ISSUE_REFRESH_MS, self._refresh_due)

    def _refresh_due(self) -> bool:
        self._refresh_src = None
        self._show_issues()
        return GLib.SOURCE_REMOVE

    def _mutate(self, change: Callable[[], Any]) -> None:
        change()
        self._load()

    def _show_issues(self) -> None:
        issues = self._s.issues()
        b = self.buffer
        b.remove_tag(self._issue_tag, b.get_start_iter(), b.get_end_iter())
        for i in issues:
            b.apply_tag(self._issue_tag, b.get_iter_at_offset(i.start),
                        b.get_iter_at_offset(i.end))
        for row in self.issue_list.get_children():
            self.issue_list.remove(row)
        text = self._s.text
        for i in issues:
            row = Gtk.Box(spacing=6, margin=2)
            label = Gtk.Label(label=f"“{text[i.start:i.end]}” — {i.message}", xalign=0,
                              ellipsize=Pango.EllipsizeMode.END)
            row.pack_start(label, True, True, 0)
            for n, r in enumerate(i.replacements[:_MAX_CHOICES]):
                fix = Gtk.Button(label=r or "(remove)")
                fix.connect("clicked", lambda _b, iid=i.id, n=n:
                            self._mutate(lambda: self._s.accept(iid, n)))
                row.pack_start(fix, False, False, 0)
            self.issue_list.add(row)
        self.issue_list.show_all()
        n = len(issues)
        self.issues_label.set_text("No issues" if n == 0 else
                                   f"{n} issue" + ("" if n == 1 else "s"))
        self.fix_all_button.set_sensitive(any(i.safe and i.replacements for i in issues))
        self.undo_button.set_sensitive(self._s.can_undo)

    # -- tone chips ------------------------------------------------------------
    def _chip(self, key: str) -> None:
        if key == "custom":
            self.custom_row.show()
            self.custom_entry.grab_focus()
            return
        self._start(key, None)

    def _run_custom(self) -> None:
        instruction = self.custom_entry.get_text().strip()
        if instruction:
            self._start("custom", instruction)

    def _start(self, key: str, custom: str | None) -> None:
        self._show_preview(Preview(key, "", None))
        self._s.preview(key, custom, on_update=lambda p: GLib.idle_add(self._on_preview, p))

    def _on_preview(self, p: Preview) -> bool:
        stale = p.outcome is None and self._s.outcome is not None  # a late partial
        if not self._done and self._s.preview_tone == p.tone and not stale:
            self._show_preview(p)
        return GLib.SOURCE_REMOVE

    def _show_preview(self, p: Preview | None) -> None:
        if p is None:
            self.preview_box.hide()
            return
        self.preview_box.show()
        o = p.outcome
        new = p.partial if o is None else (o.text or "")
        self.preview_label.set_markup(diff_markup(word_diff(self._s.text, new)) if new else "")
        if o is None:
            self.preview_status.set_text("Rewriting…")
        elif o.applicable:
            self.preview_status.set_text("")
        else:
            self.preview_status.set_text(_NOT_APPLICABLE.get(o.reason or "", "Not applicable."))
        self.use_button.set_sensitive(o is not None and o.applicable)
        self.copy_button.set_sensitive(o is not None and bool(o.text))

    def _use_preview(self) -> None:
        if self._s.apply_preview():
            self._load()

    def _copy_preview(self) -> None:
        o = self._s.outcome
        if o is not None and o.text:
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(o.text, -1)


def gtk_edit_host(grammar: Any, issue_filter: Any, supervisor: Any,
                  run_bg: RunBg) -> EditSessionHost:
    """The daemon's `edit_session` handler, with GTK windows. Main loop only."""
    current: list[EditWindow] = []

    def open_window(session: EditSession, over: Rect | None,
                    on_done: Callable[[str, str], None]) -> None:
        def done(action: str, text: str) -> None:
            current.clear()
            on_done(action, text)

        current[:] = [EditWindow(session, over, done)]

    def present_window() -> None:
        if current:
            current[0].present()

    return EditSessionHost(grammar, issue_filter, supervisor, run_bg,
                           open_window=open_window, present_window=present_window,
                           window_rect=window_rect)
