"""Tray icon over Ayatana AppIndicator; a logged no-op when its GIR is not installed."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from glance.backends.base import SIGN_IN
from glance.log import log_event
from glance.ui.onboarding_model import ORDER, TITLES, BackendRow, headline

INSTALL_HINT = "sudo apt install gir1.2-ayatanaappindicator3-0.1"
# state -> (icon name, line shown at the top of the menu)
_STATES = {
    "active": ("tools-check-spelling-symbolic", "Glance is on"),
    "paused": ("media-playback-pause-symbolic", "Glance is paused"),
    "private": ("channel-secure-symbolic", "Private field: nothing is read"),
    "degraded": ("dialog-warning-symbolic", "Glance is restarting a helper"),
    "no_ai": ("network-offline-symbolic", "Connect an AI"),
}


class BackendMenu:
    """"AI backend ▸": a radio list of the ready backends, then "Set up…". Main thread only."""

    def __init__(self, gtk: Any, on_choose: Callable[[str], None],
                 on_setup: Callable[[], None]) -> None:
        self._gtk, self._on_choose, self._on_setup = gtk, on_choose, on_setup
        self._rebuilding = False
        self.item = gtk.MenuItem(label="AI backend")
        self.submenu = gtk.Menu()
        self.item.set_submenu(self.submenu)
        self.set_rows(())

    def set_rows(self, rows: tuple[BackendRow, ...]) -> None:
        gtk = self._gtk
        self._rebuilding = True
        for child in self.submenu.get_children():
            self.submenu.remove(child)
        group = None
        ready = [r for r in rows if r.state == "ready"]
        for row in ready:
            radio = gtk.RadioMenuItem.new_with_label_from_widget(group, row.title)
            if group is None:
                group = radio
            radio.set_active(row.selected)
            radio.connect("toggled", self._toggled, row.name)
            self.submenu.append(radio)
        if not ready:
            self.submenu.append(gtk.MenuItem(label="No AI connected. Sign in to one:",
                                             sensitive=False))
            for name in ORDER:
                self.submenu.append(gtk.MenuItem(
                    label=f"{TITLES[name]}: {SIGN_IN[name]}", sensitive=False))
        self.submenu.append(gtk.SeparatorMenuItem())
        setup = gtk.MenuItem(label="Set up…")
        setup.connect("activate", lambda _i: self._on_setup())
        self.submenu.append(setup)
        self.submenu.show_all()
        self._rebuilding = False

    def _toggled(self, item: Any, name: str) -> None:
        if item.get_active() and not self._rebuilding:
            self._on_choose(name)


class GtkTray:
    """TrayPort. `available` is False when the indicator library is missing.

    Menu: status line, "AI backend ▸", "Show suggestions automatically" (a check item: on is
    `[ui] suggestions = "auto"`), Pause/Resume, Quit. `set_backends` takes rows built by the
    caller (never a blocking health check here)."""

    def __init__(self, on_pause: Callable[[], None], on_resume: Callable[[], None],
                 on_quit: Callable[[], None],
                 on_choose: Callable[[str], None] = lambda name: None,
                 on_setup: Callable[[], None] = lambda: None,
                 on_auto_suggest: Callable[[bool], None] = lambda on: None,
                 auto_suggest: bool = False,
                 on_update: Callable[[], None] = lambda: None) -> None:
        self._indicator = None
        self._on_auto_suggest = on_auto_suggest
        self._syncing = False
        self._paused = False
        self._state = "active"
        self._ai_line: str | None = None
        self._on_pause = on_pause
        self._on_resume = on_resume
        try:
            import gi

            gi.require_version("AyatanaAppIndicator3", "0.1")
            gi.require_version("Gtk", "3.0")
            from gi.repository import AyatanaAppIndicator3 as AppIndicator
            from gi.repository import Gtk
        except (ImportError, ValueError):
            log_event("tray_unavailable")  # once: this object is built once
            return
        self._indicator = AppIndicator.Indicator.new(
            "glance", _STATES["active"][0], AppIndicator.IndicatorCategory.APPLICATION_STATUS)
        self._indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self._indicator.set_title("Glance")
        menu = Gtk.Menu()
        self._headline = Gtk.MenuItem(label=_STATES["active"][1], sensitive=False)
        self._toggle = Gtk.MenuItem(label="Pause")
        self._toggle.connect("activate", self._on_toggle)
        quit_item = Gtk.MenuItem(label="Quit Glance")
        quit_item.connect("activate", lambda _item: on_quit())
        self._backends = BackendMenu(Gtk, on_choose, on_setup)
        self._auto = Gtk.CheckMenuItem(label="Show suggestions automatically")
        self._auto.set_active(auto_suggest)
        self._auto.connect("toggled", self._auto_toggled)
        self._update = Gtk.MenuItem(label="Update available \u2014 Update now")
        self._update.connect("activate", lambda _i: on_update())
        for item in (self._headline, self._update, Gtk.SeparatorMenuItem(), self._backends.item,
                     self._auto, self._toggle, quit_item):
            menu.append(item)
        menu.show_all()
        self._update.hide()
        self._indicator.set_menu(menu)

    @property
    def available(self) -> bool:
        return self._indicator is not None

    def set_update_available(self, available: bool) -> None:
        """Show or hide "Update available — Update now" (main thread)."""
        if self._indicator is not None:
            self._update.set_visible(available)

    def set_state(self, state: str) -> None:
        if self._indicator is None:
            return
        self._state = state
        self._paused = state == "paused"
        self._toggle.set_label("Resume" if self._paused else "Pause")
        self._show_state()

    def set_backends(self, rows: tuple[BackendRow, ...]) -> None:
        """The backend rows now known (main thread): refreshes the radio list and the
        status line while Glance is on."""
        if self._indicator is None:
            return
        self._ai_line = headline(rows)
        self._backends.set_rows(rows)
        self._show_state()

    def _show_state(self) -> None:
        icon, line = _STATES.get(self._state, _STATES["active"])
        if self._state == "active" and self._ai_line is not None:
            line = self._ai_line
        self._indicator.set_icon_full(icon, line)
        self._headline.set_label(line)

    def set_auto_suggest(self, on: bool) -> None:
        """Reflect `[ui] suggestions` (the config may have been edited by hand)."""
        if self._indicator is None or self._auto.get_active() == on:
            return
        self._syncing = True
        try:
            self._auto.set_active(on)
        finally:
            self._syncing = False

    def _auto_toggled(self, item: Any) -> None:
        if not self._syncing:
            self._on_auto_suggest(bool(item.get_active()))

    def _on_toggle(self, _item: object) -> None:
        (self._on_resume if self._paused else self._on_pause)()
