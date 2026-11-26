"""The Settings window (Linux, GTK 3; X11 and Wayland alike: it is an ordinary app window).

A thin renderer of `settings_model`: the model says what each section holds and does every
write; this draws it in Glance's look and hands changes back. Opened from the tray
("Settings…") inside glanced, or by `glance settings` on its own; never by itself.

Main thread only. Whatever can block (glanced over ctl.sock, systemctl, `glance doctor`) runs
on a worker thread and comes back through `GLib.idle_add`. Pages are built when first shown,
so the window opens at once.
"""
from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango

from glance.ui import settings_model as sm
from glance.ui.look import chord_keys
from glance.ui.onboarding_model import ORDER, TITLES
from glance.ui.theme import install

NAV_W = 196
PAGE_W = 680
SUBTITLES = {
    "general": "How and when Glance checks your writing.",
    "features": "Turn Glance's extras on or off. Changes apply at once.",
    "ai": "The AI Glance asks for rewrites and deeper checks, and what it costs.",
    "shortcuts": "Click a shortcut, then press the keys you want.",
    "apps": "Where Glance works, and how it writes there.",
    "privacy": "What Glance reads, and what it never does.",
    "about": "",
}


def _cls(widget: Gtk.Widget, *names: str) -> Any:
    for n in names:
        widget.get_style_context().add_class(n)
    return widget


def _label(text: str, *classes: str, wrap: bool = False, chars: int = 0) -> Gtk.Label:
    label = Gtk.Label(xalign=0, selectable=False)
    label.set_text(text)
    if wrap:
        label.set_line_wrap(True)
        label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_max_width_chars(chars or 60)
    return _cls(label, *classes)


def _mark(px: int) -> Gtk.Widget:
    from glance.ui.overlay_gtk import _mark as mark

    return mark(px)


def _keycaps(keys: list[str]) -> Gtk.Widget:
    from glance.ui.overlay_gtk import _keycaps as caps

    return caps(keys, plus=True)


class Worker:
    """`run(work, done)`: `work()` on a thread, `done(result)` back on the main loop."""

    def run(self, work: Callable[[], Any], done: Callable[[Any], None]) -> None:
        def job() -> None:
            try:
                result = work()
            except Exception:  # noqa: BLE001 - a failed request shows as "no answer"
                result = None
            GLib.idle_add(lambda: (done(result), False)[1])

        threading.Thread(target=job, daemon=True, name="glance-settings").start()


class _Row:
    """One setting row: title and help on the left, its control on the right, an error
    line under it (hidden while there is none)."""

    def __init__(self, title: str, help_text: str = "", sep: bool = True) -> None:
        self.box = _cls(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4),
                        "glance-set-row", *(("glance-sep",) if sep else ()))
        top = Gtk.Box(spacing=16)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        texts.set_valign(Gtk.Align.CENTER)
        texts.pack_start(_label(title, "glance-set-title"), False, False, 0)
        if help_text:
            texts.pack_start(_label(help_text, "glance-set-help", wrap=True, chars=52),
                             False, False, 0)
        top.pack_start(texts, True, True, 0)
        self.controls = Gtk.Box(spacing=8)
        self.controls.set_valign(Gtk.Align.CENTER)
        top.pack_end(self.controls, False, False, 0)
        self.box.pack_start(top, False, False, 0)
        self.error = _label("", "glance-set-error", wrap=True, chars=70)
        self.error.set_no_show_all(True)
        self.box.pack_start(self.error, False, False, 0)

    def add(self, widget: Gtk.Widget) -> Any:
        widget.set_valign(Gtk.Align.CENTER)
        self.controls.pack_start(widget, False, False, 0)
        return widget

    def say(self, error: str | None) -> None:
        self.error.set_text(error or "")
        self.error.set_visible(bool(error))


class SettingsWindow:
    """`show(section)` opens (or raises) the window on a section."""

    def __init__(self, model: sm.SettingsModel, services: sm.SettingsServices | None = None,
                 worker: Any = None, on_close: Callable[[], None] | None = None,
                 start_at_login: sm.StartAtLogin | None = None,
                 open_uri: Callable[[str], None] | None = None,
                 on_usage_details: Callable[[], None] | None = None) -> None:
        self.theme = install()
        self.model = model
        self.services = services if services is not None else sm.SettingsServices()
        self.worker = worker or Worker()
        self.login = start_at_login if start_at_login is not None else sm.StartAtLogin()
        self._on_close = on_close
        self._open_uri = open_uri or self._default_open
        self._usage_details = on_usage_details
        self._section = "general"
        self._pages: dict[str, Gtk.Widget] = {}
        self._capturing: tuple[str, Gtk.Button, _Row] | None = None
        self._chord_error: tuple[str, str] | None = None
        # what glanced said last (None: not asked yet / not running)
        self._backends: dict[str, Any] | None = None
        self._backends_asked = False
        self._keys: dict[str, bool] | None = None
        self._usage: dict[str, Any] | None = None
        self._live: dict[str, str] = {}
        self._login_state: Any = "unknown"
        self._doctor: list[Any] | None = None
        self._doctor_running = False

        w = self.window = Gtk.Window(title=sm.TITLE)
        w.set_default_size(NAV_W + PAGE_W + 80, 640)
        w.set_position(Gtk.WindowPosition.CENTER)
        w.set_icon_from_file(_icon_file()) if _icon_file() else None
        _cls(w, "glance-root", "glance-window", "glance-settings")
        w.connect("delete-event", self._on_delete)
        w.connect("key-press-event", self._on_key)
        w.connect("focus-in-event", self._on_focus)
        outer = Gtk.Box()
        w.add(outer)
        nav = _cls(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2), "glance-nav")
        nav.set_size_request(NAV_W, -1)
        head = Gtk.Box(spacing=10)
        head.set_margin_start(6)
        head.set_margin_bottom(14)
        head.pack_start(_mark(24), False, False, 0)
        head.pack_start(_label("Glance", "glance-title"), False, False, 0)
        nav.pack_start(head, False, False, 0)
        self._nav: dict[str, Gtk.Button] = {}
        for key, title in sm.SECTIONS:
            b = _cls(Gtk.Button(label=title, relief=Gtk.ReliefStyle.NONE), "glance-nav-item")
            b.get_child().set_xalign(0)
            b.connect("clicked", lambda _b, k=key: self.show_section(k))
            nav.pack_start(b, False, False, 0)
            self._nav[key] = b
        outer.pack_start(nav, False, False, 0)
        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.NONE)
        outer.pack_start(self._stack, True, True, 0)
        outer.show_all()
        self.theme.on_change(self._restyle)

    # -- opening -----------------------------------------------------------------------
    def show(self, section: str | None = None) -> None:
        self.model.refresh()  # the file may have changed while the window was closed
        for key in list(self._pages):
            self._drop(key)
        self.show_section(section or self._section)
        self.window.show()
        self.window.present()

    def show_section(self, key: str) -> None:
        if key not in dict(sm.SECTIONS):
            key = "general"
        self._cancel_capture()
        self._section = key
        for k, b in self._nav.items():
            ctx = b.get_style_context()
            (ctx.add_class if k == key else ctx.remove_class)("glance-on")
        if key not in self._pages:
            self._build(key)
        self._stack.set_visible_child_name(key)
        self._fetch(key)

    def _drop(self, key: str) -> None:
        page = self._pages.pop(key, None)
        if page is not None:
            self._stack.remove(page)
            page.destroy()

    def rebuild(self, key: str | None = None) -> None:
        """Draw a section again from the model (after a write that changes its shape)."""
        key = key or self._section
        scroll = self._pages.get(key)
        pos = scroll.get_vadjustment().get_value() if scroll is not None else 0.0
        self._drop(key)
        if key == self._section:
            self._build(key)
            self._stack.set_visible_child_name(key)
            new = self._pages[key]
            GLib.idle_add(lambda: (new.get_vadjustment().set_value(pos), False)[1])

    def _build(self, key: str) -> None:
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        for side in ("start", "end"):
            getattr(page, f"set_margin_{side}")(32)
        page.set_margin_top(26)
        page.set_margin_bottom(28)
        page.set_size_request(PAGE_W, -1)
        page.set_halign(Gtk.Align.FILL)
        title = dict(sm.SECTIONS)[key]
        heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        heading.pack_start(_label(title, "glance-headline"), False, False, 0)
        if SUBTITLES.get(key):
            heading.pack_start(_label(SUBTITLES[key], "glance-set-help"), False, False, 0)
        heading.set_margin_bottom(4)
        page.pack_start(heading, False, False, 0)
        if self.model.load_error:
            page.pack_start(self._banner(self.model.load_error), False, False, 0)
        getattr(self, f"_page_{key}")(page)
        scroll.add(page)
        scroll.show_all()
        self._pages[key] = scroll
        self._stack.add_named(scroll, key)

    # -- small builders ----------------------------------------------------------------
    def _group(self, page: Gtk.Box, title: str = "") -> Gtk.Box:
        if title:
            head = _label(title, "glance-section")
            head.set_margin_top(6)
            head.set_margin_start(2)
            page.pack_start(head, False, False, 0)
        group = _cls(Gtk.Box(orientation=Gtk.Orientation.VERTICAL), "glance-group")
        page.pack_start(group, False, False, 0)
        return group

    def _row(self, group: Gtk.Box, title: str, help_text: str = "") -> _Row:
        row = _Row(title, help_text, sep=bool(group.get_children()))
        group.pack_start(row.box, False, False, 0)
        return row

    def _banner(self, text: str) -> Gtk.Widget:
        box = _cls(Gtk.Box(), "glance-banner")
        box.pack_start(_label(text, wrap=True, chars=80), True, True, 0)
        return box

    def _button(self, text: str, cb: Callable[[], None], *classes: str) -> Gtk.Button:
        b = _cls(Gtk.Button(label=text), *(classes or ("glance-secondary",)))
        b.connect("clicked", lambda _b: cb())
        return b

    def _link(self, text: str, cb: Callable[[], None]) -> Gtk.Button:
        return self._button(text, cb, "glance-link", "glance-strong")

    def _switch(self, active: bool, on_change: Callable[[bool], str | None],
                row: _Row, rebuild: bool = False) -> Gtk.Switch:
        sw = Gtk.Switch(active=bool(active))
        sw.set_valign(Gtk.Align.CENTER)

        def changed(s: Gtk.Switch, _p: Any) -> None:
            error = on_change(s.get_active())
            row.say(error)
            if error is not None:
                s.handler_block_by_func(changed)
                s.set_active(not s.get_active())
                s.handler_unblock_by_func(changed)
            elif rebuild:
                GLib.idle_add(lambda: (self.rebuild(), False)[1])

        sw.connect("notify::active", changed)
        return row.add(sw)

    def _segmented(self, choices: tuple[tuple[Any, str], ...], current: Any,
                   on_pick: Callable[[Any], str | None], row: _Row) -> Gtk.Box:
        seg = _cls(Gtk.Box(), "glance-seg")
        buttons: dict[Any, Gtk.Button] = {}

        def mark(value: Any) -> None:
            for v, b in buttons.items():
                ctx = b.get_style_context()
                (ctx.add_class if v == value else ctx.remove_class)("glance-on")

        def pick(value: Any) -> None:
            error = on_pick(value)
            row.say(error)
            if error is None:
                mark(value)

        for value, text in choices:
            b = Gtk.Button(label=text)
            b.connect("clicked", lambda _b, v=value: pick(v))
            buttons[value] = b
            seg.pack_start(b, False, False, 0)
        mark(current)
        return row.add(seg)

    def _entry(self, text: str, on_apply: Callable[[str], str | None], row: _Row,
               chars: int = 18, placeholder: str = "", secret: bool = False) -> Gtk.Entry:
        e = Gtk.Entry(text=text, width_chars=chars)
        e.set_placeholder_text(placeholder)
        if secret:
            e.set_visibility(False)
            e.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        state = {"last": text}

        def apply(*_a: Any) -> bool:
            value = e.get_text()
            if value == state["last"]:
                return False
            error = on_apply(value)
            row.say(error)
            ctx = e.get_style_context()
            (ctx.add_class if error else ctx.remove_class)("glance-bad")
            if error is None:
                state["last"] = value
            return False

        e.connect("activate", apply)
        e.connect("focus-out-event", apply)
        return row.add(e)

    def _field(self, group: Gtk.Box, f: sm.Field, sensitive: bool = True) -> _Row:
        """A config field as a row with the control its kind wants."""
        m = self.model
        row = self._row(group, f.label, f.help)
        shown = sm.display(f, m.value(f.key))
        if f.kind == "switch":
            self._switch(shown, lambda on: m.set(f, on), row)
        elif f.kind == "choice":
            self._segmented(f.choices, shown, lambda v: m.set(f, v), row)
        else:
            chars = {"int": 7, "money": 9, "folder": 24, "model": 18}.get(f.kind, 16)
            placeholder = {"money": "No limit", "model": "Default"}.get(f.kind, "")
            entry = self._entry(shown, lambda text: m.set(f, text), row, chars, placeholder)
            if f.kind == "folder":
                self._folder_button(entry, row)
        row.box.set_sensitive(sensitive)
        return row

    def _folder_button(self, entry: Gtk.Entry, row: _Row) -> None:
        def choose() -> None:
            dialog = Gtk.FileChooserDialog(title="Save Glance Shots to",
                                           parent=self.window,
                                           action=Gtk.FileChooserAction.SELECT_FOLDER)
            dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                               "Choose", Gtk.ResponseType.ACCEPT)
            current = Path(os.path.expanduser(entry.get_text() or "~"))
            if current.is_dir():
                dialog.set_current_folder(str(current))
            if dialog.run() == Gtk.ResponseType.ACCEPT:
                path = dialog.get_filename() or ""
                home = os.path.expanduser("~")
                if path == home or path.startswith(home + os.sep):
                    path = "~" + path[len(home):]
                entry.set_text(path)
                entry.emit("activate")
            dialog.destroy()

        row.add(self._button("Choose…", choose))

    # -- General -----------------------------------------------------------------------
    def _page_general(self, page: Gtk.Box) -> None:
        group = self._group(page)
        for f in sm.GENERAL:
            self._field(group, f)
        group = self._group(page, "Startup")
        row = self._row(group, "Start Glance at login",
                        "Runs Glance when you log in (" +
                        ("a LaunchAgent" if self.login.mac else "the glance.service user unit")
                        + "). Turning it off does not stop Glance now.")
        state = self._login_state
        sw = Gtk.Switch(active=state is True)
        sw.set_valign(Gtk.Align.CENTER)
        sw.set_sensitive(state in (True, False))
        if state is None:
            row.say("Glance is not installed as a login service on this computer.")

        def changed(s: Gtk.Switch, _p: Any) -> None:
            want = s.get_active()
            s.set_sensitive(False)

            def done(ok: Any) -> None:
                s.set_sensitive(True)
                if ok:
                    self._login_state = want
                    row.say(None)
                else:
                    row.say("Could not change it.")
                    s.handler_block_by_func(changed)
                    s.set_active(not want)
                    s.handler_unblock_by_func(changed)

            self.worker.run(lambda: self.login.set(want), done)

        sw.connect("notify::active", changed)
        row.add(sw)

    # -- Features ----------------------------------------------------------------------
    def _page_features(self, page: Gtk.Box) -> None:
        m = self.model
        for feature, on, fields in m.features():
            group = self._group(page)
            row = self._row(group, feature.label, _feature_help(feature))
            self._switch(on, lambda v, n=feature.name: m.set_feature(n, v), row, rebuild=True)
            for f in fields:
                self._field(group, f, sensitive=on)

    # -- AI ----------------------------------------------------------------------------
    def _page_ai(self, page: Gtk.Box) -> None:
        m = self.model
        info = self._backends
        if info is None and self._backends_asked:
            page.pack_start(self._banner(
                "Glance is not running, so the AI tools cannot be checked. Start it, then "
                "open Settings again."), False, False, 0)
        head = Gtk.Box()
        head.pack_start(_label("AI backends", "glance-section"), True, True, 0)
        head.pack_end(self._link("Check again", lambda: self._ask_backends(True)),
                      False, False, 0)
        head.set_margin_top(6)
        head.set_margin_start(2)
        page.pack_start(head, False, False, 0)
        group = self._group(page)
        choice = (info or {}).get("choice") if info else m.cfg.backend.choice
        states = (info or {}).get("backends") or {}
        same = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)  # the Use / In use column
        for name in ORDER:
            status = (sm.backend_status(states.get(name)) if info
                      else ("Checking…" if not self._backends_asked else "Unknown"))
            ready = info is not None and states.get(name, {}).get("installed") and \
                states.get(name, {}).get("signed_in") is not False
            if name == "api" and info is not None and self._keys is not None:
                status = "Key saved" if any(self._keys.values()) else "No key"
            row = self._row(group, TITLES[name], status)
            if name in sm.MODEL_BACKENDS:
                f = sm.model_field(name)
                current = m.value(f.key) if m.is_set(f.key) else ""
                self._entry(current, lambda t, f=f: m.set(f, t), row, 14,
                            placeholder=str(m.value(f.key) or "Default"))
            if name == choice:
                tail = row.add(_label("In use", "glance-tag", "glance-on"))
                tail.set_xalign(0.5)
            else:
                tail = row.add(self._button("Use", lambda n=name, r=row: self._use(n, r)))
                tail.set_sensitive(bool(ready))
            same.add_widget(tail)
        self._api_group(page)
        group = self._group(page, "Spending")
        self._field(group, sm.COST_LIMIT)
        summary = sm.usage_summary(self._usage)
        row = self._row(group, "AI usage", "\n".join(
            x for x in (summary.today, summary.month, summary.limit) if x))
        row.add(self._button("Details…", self._details))

    def _api_group(self, page: Gtk.Box) -> None:
        m = self.model
        group = self._group(page, "API key")
        self._field(group, sm.API_PROVIDER)
        self._field(group, sm.API_MODEL)
        provider = m.cfg.backend.api_provider
        saved = (self._keys or {}).get(provider)
        help_text = ("A key is saved in the system keyring." if saved else
                     "Paste a key; it goes to the system keyring and is never shown again.")
        row = self._row(group, "Key", help_text)
        entry = Gtk.Entry(width_chars=16, visibility=False)
        entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        entry.set_placeholder_text("••••••••" if saved else "Paste the key")
        row.add(entry)

        def save() -> None:
            key = entry.get_text().strip()
            entry.set_text("")  # never kept in the widget
            if not key:
                row.say("Paste a key first.")
                return
            self.worker.run(lambda: self.services.set_key(provider, key),
                            lambda r: self._key_done(r, row))

        def remove() -> None:
            self.worker.run(lambda: self.services.remove_key(provider),
                            lambda r: self._key_done(r, row))

        entry.connect("activate", lambda _e: save())
        row.add(self._button("Save", save))
        if saved:
            row.add(self._button("Remove", remove))

    def _key_done(self, reply: Any, row: _Row) -> None:
        if reply is None:
            row.say("Glance is not running: start it to change the key.")
            return
        if not reply.get("ok"):
            row.say(f"Not saved: {reply.get('error', 'refused')}")
            return
        self._ask_keys()

    def _use(self, name: str, row: _Row) -> None:
        def done(reply: Any) -> None:
            if reply is None:
                row.say("Glance is not running.")
            elif not reply.get("ok"):
                row.say("Not ready yet: sign in to it first.")
            else:
                self._ask_backends(False)

        self.worker.run(lambda: self.services.use_backend(name), done)

    def _details(self) -> None:
        if self._usage_details is not None:
            self._usage_details()
            return
        from glance.backends.usage import render_report
        from glance.ui.usage_gtk import UsageWindow

        def show(report: Any) -> None:
            if not hasattr(self, "_usage_window"):
                self._usage_window = UsageWindow()
            self._usage_window.window.set_transient_for(self.window)
            self._usage_window.show(render_report(report or {"periods": {}}))

        self.worker.run(self.services.usage, show)

    # -- Shortcuts ---------------------------------------------------------------------
    def _page_shortcuts(self, page: Gtk.Box) -> None:
        group = self._group(page)
        told, self._chord_error = self._chord_error, None
        for r in self.model.shortcuts(self._live):
            row = self._row(group, r.label)
            row.say(told[1] if told and told[0] == r.name else r.problem)
            cap = _cls(Gtk.Button(), "glance-capture")
            cap.add(_keycaps(chord_keys(r.chord, self.model.mac)))
            cap.connect("clicked", lambda b, n=r.name, rw=row: self._capture(n, b, rw))
            row.add(cap)
            reset = row.add(self._link("Reset", lambda n=r.name, rw=row: self._reset_chord(
                n, rw)))
            reset.set_opacity(0.0 if r.is_default else 1.0)
            reset.set_sensitive(not r.is_default)
        page.pack_start(_label(
            "Shortcuts apply at once. Esc cancels; Backspace while waiting puts the default "
            "back. Glance checks each one against the desktop's own shortcuts.",
            "glance-set-help", wrap=True, chars=90), False, False, 0)

    def _capture(self, name: str, button: Gtk.Button, row: _Row) -> None:
        self._cancel_capture()
        self._capturing = (name, button, row)
        child = button.get_child()
        button.remove(child)
        child.destroy()
        button.add(_label("Press the keys…"))
        button.show_all()
        _cls(button, "glance-on")
        button.grab_focus()
        row.say(None)

    def _cancel_capture(self) -> None:
        if self._capturing is not None:
            self._capturing = None
            if self._section == "shortcuts":
                GLib.idle_add(lambda: (self.rebuild("shortcuts"), False)[1])

    def _reset_chord(self, name: str, row: _Row) -> None:
        error = self.model.reset_shortcut(name)
        row.say(error)
        if error is None:
            self.rebuild("shortcuts")

    def _on_key(self, _w: Gtk.Window, event: Gdk.EventKey) -> bool:
        if self._capturing is None:
            if event.keyval == Gdk.KEY_Escape or (
                    event.keyval in (Gdk.KEY_w, Gdk.KEY_W)
                    and event.state & Gdk.ModifierType.CONTROL_MASK):
                self.close()
                return True
            return False
        name, _button, row = self._capturing
        state = event.state
        mods = [m for m, mask in (("ctrl", Gdk.ModifierType.CONTROL_MASK),
                                  ("shift", Gdk.ModifierType.SHIFT_MASK),
                                  ("alt", Gdk.ModifierType.MOD1_MASK),
                                  ("super", Gdk.ModifierType.SUPER_MASK | Gdk.ModifierType.MOD4_MASK))
                if state & mask]
        if not mods and event.keyval == Gdk.KEY_Escape:
            self._cancel_capture()
            return True
        if not mods and event.keyval == Gdk.KEY_BackSpace:
            self._capturing = None
            self._reset_chord(name, row)
            return True
        keyname = Gdk.keyval_name(Gdk.keyval_to_lower(event.keyval)) or ""
        chord = sm.chord_from_keys(mods, keyname)
        if chord is None:
            return True  # a modifier on its own: wait for the key
        return self.apply_chord(name, chord)

    def apply_chord(self, name: str, chord: str) -> bool:
        """The captured `chord` for `name`: written, or its problem shown under the row."""
        error = self.model.set_shortcut(name, chord)
        self._capturing = None
        self._chord_error = None if error is None else (name, f"{chord}: {error}")
        self.rebuild("shortcuts")
        return True

    # -- Apps --------------------------------------------------------------------------
    def _page_apps(self, page: Gtk.Box) -> None:
        m = self.model
        group = self._group(page, "Never in these apps")
        self._list_rows(group, "app_denylist",
                        "Glance never reads a field in an app whose name contains one of "
                        "these. The built-in ones (password managers, remote desktops) always "
                        "stay.", "Part of the app's name, e.g. signal", "Add a name")
        group = self._group(page, "Per-app settings")
        profiles = m.profiles()
        apps = m.apps()
        if not apps:
            self._row(group, "No app has its own settings yet",
                      "Add one by its window class to switch Glance off there or pick the "
                      "audience its tones are written for.")
        for app in apps:
            row = self._row(group, app.wm_class,
                            "Glance checks here" if app.capture else "Glance is off here")
            self._switch(app.capture, lambda on, a=app.wm_class: m.set_app_capture(a, on),
                         row, rebuild=True)
            row.add(self._picker(
                (("", "Automatic"), *profiles), app.profile or "",
                lambda v, a=app.wm_class, r=row: self._after(
                    m.set_app_profile(a, v or None), r), "Audience: "))
            row.add(self._link("Remove", lambda a=app.wm_class, r=row: self._after(
                m.remove_app(a), r)))
        row = self._row(group, "Add an app", "By its window class, e.g. slack, code or "
                                             "org.gnome.texteditor.")
        self._adder(row, m.add_app, "Window class")

    def _picker(self, choices: tuple[tuple[str, str], ...], current: str,
                on_pick: Callable[[str], None], lead: str = "") -> Gtk.Widget:
        """A button showing the current choice that opens a list of them (a popover: it is
        drawn inside this window, so it follows Glance's look in light and dark)."""
        names = dict(choices)
        button = _cls(Gtk.MenuButton(), "glance-secondary")
        button.add(_label(f"{lead}{names.get(current, current)}  ▾"))
        pop = _cls(Gtk.Popover(), "glance-root")
        box = _cls(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1), "glance-menu")
        for value, text in choices:
            item = _cls(Gtk.Button(label=text), "glance-item",
                        *(("glance-on",) if value == current else ()))
            item.get_child().set_xalign(0)
            item.connect("clicked", lambda _b, v=value: (pop.popdown(), on_pick(v)))
            box.pack_start(item, False, False, 0)
        box.show_all()
        pop.add(box)
        button.set_popover(pop)
        return button

    def _after(self, error: str | None, row: _Row) -> None:
        row.say(error)
        if error is None:
            self.rebuild()

    def _adder(self, row: _Row, add: Callable[[str], str | None], placeholder: str) -> None:
        entry = Gtk.Entry(width_chars=18)
        entry.set_placeholder_text(placeholder)
        row.add(entry)

        def go() -> None:
            self._after(add(entry.get_text()), row)

        entry.connect("activate", lambda _e: go())
        row.add(self._button("Add", go))

    def _list_rows(self, group: Gtk.Box, name: str, help_text: str, placeholder: str,
                   add_title: str) -> None:
        m = self.model
        items = m.list_items(name)
        builtin = [i.value for i in items if i.builtin]
        if builtin:
            row = self._row(group, "Built in", help_text)
            flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=6,
                               column_spacing=6, row_spacing=6, homogeneous=False)
            for v in builtin:
                tag = _label(v, "glance-tag")
                tag.set_halign(Gtk.Align.START)
                flow.add(tag)
            row.box.pack_start(flow, False, False, 4)
            help_text = ""
        for item in items:
            if item.builtin:
                continue
            row = self._row(group, item.value)
            row.add(self._link("Remove", lambda v=item.value, r=row: self._after(
                m.remove_from_list(name, v), r)))
        row = self._row(group, add_title, help_text)
        self._adder(row, lambda text: m.add_to_list(name, text), placeholder)

    # -- Privacy -----------------------------------------------------------------------
    def _page_privacy(self, page: Gtk.Box) -> None:
        group = self._group(page, "What Glance never does")
        box = _cls(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8), "glance-set-row")
        for line in sm.PRIVACY_TEXT:
            item = Gtk.Box(spacing=10)
            dot = _label("•", "glance-set-ok")
            dot.set_valign(Gtk.Align.START)
            item.pack_start(dot, False, False, 0)
            item.pack_start(_label(line, "glance-what", wrap=True, chars=72), True, True, 0)
            box.pack_start(item, False, False, 0)
        group.pack_start(box, False, False, 0)
        group = self._group(page, "Pages Glance never reads")
        self._list_rows(group, "url_denylist",
                        "Web pages whose address matches one of these patterns (regular "
                        "expressions): sign-in, banking and checkout pages.",
                        r"e.g. intranet\.example\.com", "Add a pattern")
        group = self._group(page, "Programs allowed in terminals")
        self._list_rows(group, "terminal_allowlist",
                        "Terminals are never read, except while one of these programs runs "
                        "in the foreground.", "e.g. claude", "Add a program")
        page.pack_start(_label(
            f"Everything here is stored in {self.model.path}.", "glance-set-help", wrap=True,
            chars=90), False, False, 0)

    # -- About -------------------------------------------------------------------------
    def _page_about(self, page: Gtk.Box) -> None:
        group = self._group(page)
        top = _cls(Gtk.Box(spacing=14), "glance-set-row")
        top.pack_start(_mark(44), False, False, 0)
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        names.set_valign(Gtk.Align.CENTER)
        names.pack_start(_label("Glance", "glance-title"), False, False, 0)
        names.pack_start(_label(f"Version {sm.glance_version()} · a writing assistant for "
                                "every app", "glance-set-help"), False, False, 0)
        top.pack_start(names, True, True, 0)
        group.pack_start(top, False, False, 0)
        row = self._row(group, "Source code and updates", sm.GITHUB_URL)
        row.add(self._button("Open GitHub", lambda: self._open_uri(sm.GITHUB_URL)))
        row = self._row(group, "Report a problem",
                        "Opens the issue list on GitHub. Say what you did and what happened; "
                        "never paste private text.")
        row.add(self._button("Report…", lambda: self._open_uri(sm.ISSUES_URL)))
        group = self._group(page, "Health check")
        if self._doctor_running:
            summary = "Checking…"
        elif self._doctor is None:
            summary = "Not run yet. The same checks as `glance doctor`."
        else:
            summary = sm.doctor_summary(self._doctor)
        row = self._row(group, "Setup checks", summary)
        run = row.add(self._button("Run checks", self._run_doctor, "glance-primary"))
        run.set_sensitive(not self._doctor_running)
        mark = {"ok": "✓", "warn": "!", "fail": "✗"}
        for r in self._doctor or ():
            line = self._row(group, f"{mark.get(r.status, '?')}  {r.title}", r.detail)
            if r.status != "ok" and r.hint:
                line.say(r.hint.splitlines()[0])

    def _run_doctor(self) -> None:
        self._doctor_running = True
        self.rebuild("about")

        def done(results: Any) -> None:
            self._doctor_running = False
            self._doctor = list(results or [])
            self.rebuild("about")

        self.worker.run(self.services.doctor, done)

    # -- what glanced knows (worker threads) -------------------------------------------
    def _fetch(self, key: str) -> None:
        if key == "ai":
            if not self._backends_asked:
                self._ask_backends(False)
            self.worker.run(self.services.usage, self._got_usage)
        elif key == "shortcuts":
            self.worker.run(self.services.status, self._got_status)
        elif key == "general" and self._login_state == "unknown":
            self.worker.run(self.login.get, self._got_login)

    def _ask_backends(self, refresh: bool) -> None:
        def done(reply: Any) -> None:
            self._backends_asked = True
            self._backends = reply if reply and reply.get("ok") else None
            self._ask_keys()

        self.worker.run(lambda: self.services.backends(refresh), done)

    def _ask_keys(self) -> None:
        def done(reply: Any) -> None:
            self._keys = (reply or {}).get("keys") if reply and reply.get("ok") else None
            if self._section == "ai":
                self.rebuild("ai")
            else:
                self._drop("ai")

        self.worker.run(self.services.keys, done)

    def _got_usage(self, reply: Any) -> None:
        self._usage = reply if reply and reply.get("ok", True) else None
        if self._section == "ai" and self._backends_asked:
            self.rebuild("ai")

    def _got_status(self, reply: Any) -> None:
        live = sm.parse_live_problems((reply or {}).get("hotkeys_problems"))
        if live != self._live:
            self._live = live
            if self._section == "shortcuts" and self._capturing is None:
                self.rebuild("shortcuts")

    def _got_login(self, state: Any) -> None:
        self._login_state = state
        if self._section == "general":
            self.rebuild("general")
        else:
            self._drop("general")

    def _on_focus(self, *_a: Any) -> bool:
        """Back in the window: if config.toml changed meanwhile (the tray, an editor),
        draw it again. The window's own writes have already refreshed the model."""
        before = self.model.cfg
        self.model.refresh()
        if self.model.cfg != before and self._capturing is None:
            GLib.idle_add(self._redraw_all)
        return False

    def _redraw_all(self) -> bool:
        for key in list(self._pages):
            if key != self._section:
                self._drop(key)
        self.rebuild(self._section)
        return False

    # -- closing -----------------------------------------------------------------------
    def _restyle(self) -> None:
        self.window.queue_draw()

    def close(self) -> None:
        self._capturing = None
        self.window.hide()
        if self._on_close is not None:
            self._on_close()

    def _on_delete(self, _w: Gtk.Window, _e: Gdk.Event) -> bool:
        self.close()
        return True

    def _default_open(self, uri: str) -> None:
        try:
            Gtk.show_uri_on_window(self.window, uri, Gdk.CURRENT_TIME)
        except GLib.Error:
            pass


def _feature_help(f: Any) -> str:
    return {
        "selection": "A small Glance mark beside text you select anywhere: explain, "
                     "translate, rewrite. The selection shortcut works either way.",
        "shot": "Capture a screen region and act on it: extract text, explain, ask.",
        "autocomplete": "Ghost text after the caret while you type; Tab takes it. Unset, it "
                        "is on only with an API key backend.",
    }.get(f.name, f.note)


def _walk(widget: Gtk.Widget):  # type: ignore[no-untyped-def]
    yield widget
    if isinstance(widget, Gtk.Container):
        for child in widget.get_children():
            yield from _walk(child)


def _icon_file() -> str | None:
    from glance.ui.theme import mark_path

    path = mark_path(64)
    return path if Path(path).exists() else None


def main(section: str | None = None) -> int:
    """`glance settings` while glanced is not running: the window on its own."""
    from glance.audience.overrides import Overrides
    from glance.paths import config_path, overrides_path

    model = sm.SettingsModel(config_path(), overrides=Overrides(overrides_path()))
    win = SettingsWindow(model, on_close=Gtk.main_quit)
    win.show(section)
    Gtk.main()
    return 0
