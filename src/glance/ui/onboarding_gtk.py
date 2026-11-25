"""The "Connect an AI" window. A normal window: it may take focus (it is never in front of a
text field). Nothing here blocks the GTK main loop: `rows_provider`, `on_test` and `on_set_key`
run on worker threads and their results come back through `GLib.idle_add`."""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk

from glance.log import log_event
from glance.ui.onboarding_model import BackendRow, headline
from glance.ui.theme import install

TEST_NOTE = "Test sends one short sample sentence"
INTRO = ("Glance checks grammar on your computer. For tone and rewrites it needs one AI that "
         "you choose. Connect one, then press Use this.")
PROVIDERS = ("anthropic", "openai")
_MARK = {"ready": "●  Ready", "signed_out": "○  Signed out", "not_installed": "○  Not installed",
         "unknown": "◐  Not checked yet"}


def _style(widget: Any, *classes: str) -> Any:
    for c in classes:
        widget.get_style_context().add_class(c)
    return widget


def _in_thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True, name="glance-onboarding").start()


class _RowWidgets:
    def __init__(self, row: BackendRow) -> None:
        self.row = row
        self.frame = Gtk.Frame()
        self.state = Gtk.Label(xalign=0)
        self.command = Gtk.Label(label=row.command, xalign=0, selectable=True)
        self.copy = Gtk.Button(label="Copy")
        self.test = Gtk.Button(label="Test")
        self.result = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.use = Gtk.Button(label="Use this")
        self.note = Gtk.Label(label=row.storage_note, xalign=0, wrap=True)
        self.extra: dict[str, Any] = {}


class OnboardingWindow:
    def __init__(
        self,
        rows_provider: Callable[[], tuple[BackendRow, ...]],
        on_use: Callable[[str], None],
        on_test: Callable[[str, Callable[[bool, str], None]], None],
        on_set_key: Callable[[str, str], None],
        on_api_settings: Callable[[str, str, str], None] | None = None,
        api_settings: Callable[[], tuple[str, str, str]] = lambda: ("anthropic", "", ""),
    ) -> None:
        self._rows_provider, self._on_use, self._on_test = rows_provider, on_use, on_test
        self._on_set_key, self._on_api_settings = on_set_key, on_api_settings
        self._api_settings = api_settings
        self._widgets: dict[str, _RowWidgets] = {}
        self._refreshing = False
        self._again = False
        self._closed = False
        install()  # the same look as the strip and its drawer
        w = self.window = Gtk.Window(title="Connect an AI")
        w.set_default_size(580, 640)
        w.set_border_width(20)
        _style(w, "glance-root", "glance-window")
        w.connect("delete-event", self._on_delete)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        w.add(box)
        self.headline = _style(Gtk.Label(label="Connect an AI", xalign=0), "glance-headline")
        box.pack_start(self.headline, False, False, 0)
        intro = Gtk.Label(label=INTRO, xalign=0, wrap=True)
        box.pack_start(intro, False, False, 0)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        scroll.add(self._list)
        box.pack_start(scroll, True, True, 0)
        bar = Gtk.Box(spacing=8)
        self.recheck = _style(Gtk.Button(label="Re-check"), "glance-secondary")
        self.recheck.connect("clicked", lambda _b: self.refresh())
        self.close_button = _style(Gtk.Button(label="Close"), "glance-secondary")
        self.close_button.connect("clicked", lambda _b: self.hide())
        bar.pack_start(self.recheck, False, False, 0)
        bar.pack_end(self.close_button, False, False, 0)
        box.pack_start(bar, False, False, 0)
        box.show_all()

    # -- public ---------------------------------------------------------------
    def show(self) -> None:
        """Open (or raise) the window and re-check in the background."""
        self.window.show()
        self.window.present()
        self.refresh()

    def hide(self) -> None:
        self.window.hide()

    def close(self) -> None:
        self._closed = True
        self.window.destroy()

    def refresh(self) -> None:
        if self._closed:
            return
        if self._refreshing:
            self._again = True  # asked while one runs: run once more when it ends
            return
        self._refreshing = True
        self.recheck.set_sensitive(False)

        def work() -> None:
            try:
                rows = self._rows_provider()
            except Exception:  # noqa: BLE001 - keep what is shown; the next Re-check retries
                rows = None
            GLib.idle_add(self._rows_ready, rows)

        _in_thread(work)

    def set_rows(self, rows: tuple[BackendRow, ...]) -> None:
        """Show `rows` now. Main thread only."""
        self._apply(rows)

    # -- internals --------------------------------------------------------------
    def _on_delete(self, _w: Gtk.Window, _e: Gdk.Event) -> bool:
        self.hide()
        return True  # keep the window: the tray opens it again

    def _rows_ready(self, rows: tuple[BackendRow, ...] | None) -> bool:
        self._refreshing = False
        if self._closed:
            return False
        if self._again:
            self._again = False
            self.refresh()
        self.recheck.set_sensitive(True)
        if rows is not None:
            self._apply(rows)
        return False

    def _apply(self, rows: tuple[BackendRow, ...]) -> None:
        self.headline.set_text(headline(rows))
        for row in rows:
            widgets = self._widgets.get(row.name) or self._build(row)
            widgets.row = row
            widgets.state.set_text(_MARK[row.state])
            widgets.use.set_sensitive(row.selectable and not row.selected)
            widgets.use.set_label("In use" if row.selected else "Use this")
            widgets.test.set_sensitive(row.state != "not_installed")

    def _build(self, row: BackendRow) -> _RowWidgets:
        widgets = _RowWidgets(row)
        self._widgets[row.name] = widgets
        box = _style(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8), "glance-card")
        widgets.frame.add(box)
        _style(widgets.use, "glance-primary")
        _style(widgets.test, "glance-secondary")
        _style(widgets.copy, "glance-secondary")
        top = Gtk.Box(spacing=8)
        title = _style(Gtk.Label(label=row.title, xalign=0), "glance-title")
        top.pack_start(title, False, False, 0)
        top.pack_end(widgets.state, False, False, 0)
        box.pack_start(top, False, False, 0)
        cmd = Gtk.Box(spacing=8)
        widgets.command.get_style_context().add_class("monospace")
        widgets.command.set_markup(f"<tt>{GLib.markup_escape_text(row.command)}</tt>")
        widgets.command.set_selectable(True)
        cmd.pack_start(widgets.command, True, True, 0)
        widgets.copy.connect("clicked", lambda _b, r=row: self._copy(r.command))
        cmd.pack_end(widgets.copy, False, False, 0)
        box.pack_start(cmd, False, False, 0)
        actions = Gtk.Box(spacing=8)
        widgets.test.connect("clicked", lambda _b, n=row.name: self._test(n))
        widgets.use.connect("clicked", lambda _b, n=row.name: self._use(n))
        actions.pack_start(widgets.test, False, False, 0)
        actions.pack_start(widgets.result, True, True, 0)
        actions.pack_end(widgets.use, False, False, 0)
        box.pack_start(actions, False, False, 0)
        hint = _style(Gtk.Label(label=TEST_NOTE, xalign=0), "glance-meta")
        box.pack_start(hint, False, False, 0)
        if row.name == "api":
            box.pack_start(self._api_form(widgets), False, False, 0)
        _style(widgets.note, "glance-meta")
        _style(widgets.state, "glance-meta")
        box.pack_start(widgets.note, False, False, 0)
        self._list.pack_start(widgets.frame, False, False, 0)
        widgets.frame.show_all()
        return widgets

    def _api_form(self, widgets: _RowWidgets) -> Gtk.Widget:
        provider, model, base_url = self._api_settings()
        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        combo = Gtk.ComboBoxText()
        for p in PROVIDERS:
            combo.append_text(p)
        combo.set_active(PROVIDERS.index(provider) if provider in PROVIDERS else 0)
        model_entry = Gtk.Entry(text=model, placeholder_text="model (optional for anthropic)")
        url_entry = Gtk.Entry(text=base_url, placeholder_text="base URL (optional)")
        key_entry = Gtk.Entry(placeholder_text="API key")
        key_entry.set_visibility(False)
        key_entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        save = Gtk.Button(label="Save key")
        status = Gtk.Label(xalign=0)
        for i, (label, widget) in enumerate((("Provider", combo), ("Model", model_entry),
                                             ("Base URL", url_entry), ("Key", key_entry))):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, i, 1, 1)
            widget.set_hexpand(True)
            grid.attach(widget, 1, i, 1, 1)
        grid.attach(save, 1, 4, 1, 1)
        grid.attach(status, 1, 5, 1, 1)
        save.connect("clicked", lambda _b: self._save_key(
            combo.get_active_text() or PROVIDERS[0], model_entry.get_text(),
            url_entry.get_text(), key_entry, status))
        widgets.extra = {"provider": combo, "model": model_entry, "base_url": url_entry,
                         "key": key_entry, "save": save, "status": status}
        return grid

    def _copy(self, text: str) -> None:
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)

    def _use(self, name: str) -> None:
        def work() -> None:
            try:
                self._on_use(name)
            except Exception as e:  # noqa: BLE001
                log_event("onboarding_use_failed", kind=type(e).__name__)
                GLib.idle_add(self._set_result, name, "Could not switch")
            GLib.idle_add(self.refresh)

        _in_thread(work)

    def _set_result(self, name: str, text: str) -> bool:
        if not self._closed:
            self._widgets[name].result.set_text(text)
        return False

    def _test(self, name: str) -> None:
        widgets = self._widgets[name]
        widgets.test.set_sensitive(False)
        widgets.result.set_text("Testing…")

        def done(ok: bool, detail: str) -> None:  # any thread
            GLib.idle_add(self._tested, name, ok, detail)

        def work() -> None:
            try:
                self._on_test(name, done)
            except Exception:  # noqa: BLE001
                done(False, "test failed")

        _in_thread(work)

    def _tested(self, name: str, ok: bool, detail: str) -> bool:
        if not self._closed:
            widgets = self._widgets[name]
            widgets.test.set_sensitive(True)
            widgets.result.set_text(f"{'✓' if ok else '✗'} {detail}")
            self.refresh()
        return False

    def _save_key(self, provider: str, model: str, base_url: str, key_entry: Gtk.Entry,
                  status: Gtk.Label) -> None:
        key = key_entry.get_text().strip()
        key_entry.set_text("")  # the key leaves the widget at once
        if not key:
            status.set_text("Enter a key first")
            return
        status.set_text("Saving…")

        def work() -> None:
            try:
                if self._on_api_settings is not None:
                    self._on_api_settings(provider, model, base_url)
                self._on_set_key(provider, key)
                msg = "Saved in the system keyring"
            except Exception:  # noqa: BLE001
                msg = "Could not save the key"
            GLib.idle_add(self._saved, status, msg)

        _in_thread(work)

    def _saved(self, status: Gtk.Label, msg: str) -> bool:
        if not self._closed:
            status.set_text(msg)
            self.refresh()
        return False
