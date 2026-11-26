"""The Settings window on macOS: the same sections as the Linux one (`settings_gtk`), drawn
with plain AppKit controls in a tab view. A thin renderer of `glance.ui.settings_model`, which
does every check and write. Main thread only; glanced requests and `glance doctor` run on a
worker thread and come back with `AppHelper.callAfter`. Needs PyObjC.

Simpler than the GTK window in one place: a shortcut is typed in Glance's spelling
(`Super+Alt+g`; Super is ⌘, Alt ⌥, Ctrl ⌃) rather than captured from the keys.
"""
from __future__ import annotations

import os
import threading
from collections.abc import Callable
from typing import Any

from AppKit import (
    NSApp,
    NSApplication,
    NSBackingStoreBuffered,
    NSButton,
    NSOpenPanel,
    NSPopUpButton,
    NSScrollView,
    NSSecureTextField,
    NSTabView,
    NSTabViewItem,
    NSTextField,
    NSWindow,
    NSWorkspace,
)
from Foundation import NSURL, NSMakeRect, NSObject
from PyObjCTools import AppHelper

from glance.platform.macos.appkit_util import Target, color, tokens
from glance.platform.macos.overlay import _Flipped, _label
from glance.ui import settings_model as sm
from glance.ui.onboarding_model import ORDER, TITLES

_STYLE = 1 | 2 | 4 | 8  # titled, closable, miniaturizable, resizable
W, H = 760.0, 600.0
PAD, ROW_GAP, CONTROL_W = 20.0, 12.0, 250.0
_CHECKBOX = 3  # NSButtonTypeSwitch
_BEZEL_ROUNDED = 1


class _Page:
    """Lays rows out top to bottom in a flipped view."""

    def __init__(self, width: float, target: Any) -> None:
        self.view = _Flipped.alloc().initWithFrame_(NSMakeRect(0, 0, width, 10))
        self.w, self.y, self.t = width, PAD, target
        self.tk = tokens()

    def heading(self, text: str, size: float = 13.0) -> None:
        lab = _label(text, color(self.tk["muted"]), self.w - 2 * PAD, bold=True, lines=1,
                     size=size)
        lab.setFrameOrigin_((PAD, self.y))
        self.view.addSubview_(lab)
        self.y += lab.frame().size.height + 8

    def text(self, text: str, muted: bool = True) -> None:
        lab = _label(text, color(self.tk["muted" if muted else "body"]), self.w - 2 * PAD,
                     size=12)
        lab.setFrameOrigin_((PAD, self.y))
        self.view.addSubview_(lab)
        self.y += lab.frame().size.height + ROW_GAP

    def row(self, title: str, help_text: str, controls: list[Any],
            error: str | None = None) -> None:
        text_w = self.w - 2 * PAD - CONTROL_W - 12
        top = self.y
        lab = _label(title, color(self.tk["text"]), text_w, lines=2, size=13)
        lab.setFrameOrigin_((PAD, top))
        self.view.addSubview_(lab)
        h = lab.frame().size.height
        if help_text:
            hl = _label(help_text, color(self.tk["muted"]), text_w, size=11.5)
            hl.setFrameOrigin_((PAD, top + h + 2))
            self.view.addSubview_(hl)
            h += hl.frame().size.height + 2
        x = self.w - PAD
        ch = 0.0
        for c in reversed(controls):
            c.sizeToFit() if not isinstance(c, NSTextField) else None
            f = c.frame()
            cw = f.size.width if not isinstance(c, NSTextField) else min(160.0, CONTROL_W)
            x -= cw
            c.setFrame_(NSMakeRect(x, top, cw, max(f.size.height, 22)))
            self.view.addSubview_(c)
            x -= 6
            ch = max(ch, max(f.size.height, 22))
        self.y = top + max(h, ch)
        if error:
            el = _label(error, color(self.tk["warn_text"] if not _dark() else
                                     self.tk["dot_warn"]), self.w - 2 * PAD, size=11.5)
            el.setFrameOrigin_((PAD, self.y + 3))
            self.view.addSubview_(el)
            self.y += el.frame().size.height + 3
        self.y += ROW_GAP

    def finish(self) -> Any:
        self.view.setFrameSize_((self.w, self.y + PAD))
        return self.view

    # -- controls --------------------------------------------------------------------
    def button(self, title: str, fn: Callable[[], None]) -> Any:
        b = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 80, 24))
        b.setTitle_(title)
        b.setBezelStyle_(_BEZEL_ROUNDED)
        b.setTarget_(self.t)
        b.setAction_("fire:")
        b.setTag_(self.t.add(fn))
        return b

    def checkbox(self, on: bool, fn: Callable[[bool], None]) -> Any:
        b = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 24, 22))
        b.setButtonType_(_CHECKBOX)
        b.setTitle_("")
        b.setState_(1 if on else 0)
        b.setTarget_(self.t)
        b.setAction_("fire:")
        b.setTag_(self.t.add(lambda: fn(bool(b.state()))))
        return b

    def popup(self, choices: tuple[tuple[Any, str], ...], current: Any,
              fn: Callable[[Any], None]) -> Any:
        p = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 160, 24), False)
        values = [v for v, _ in choices]
        for _v, text in choices:
            p.addItemWithTitle_(text)
        if current in values:
            p.selectItemAtIndex_(values.index(current))
        p.setTarget_(self.t)
        p.setAction_("fire:")
        p.setTag_(self.t.add(lambda: fn(values[int(p.indexOfSelectedItem())])))
        return p

    def field(self, text: str, fn: Callable[[str], None], placeholder: str = "",
              secret: bool = False) -> Any:
        cls = NSSecureTextField if secret else NSTextField
        f = cls.alloc().initWithFrame_(NSMakeRect(0, 0, 160, 22))
        f.setStringValue_(text)
        f.setPlaceholderString_(placeholder)
        f.setTarget_(self.t)
        f.setAction_("fire:")  # Return (and leaving the field)
        f.setTag_(self.t.add(lambda: fn(str(f.stringValue()))))
        return f


def _dark() -> bool:
    from glance.platform.macos.appkit_util import is_dark

    return is_dark()


class SettingsWindow:
    """`show(section)`: open (or raise) the window on a section."""

    def __init__(self, model: sm.SettingsModel, services: sm.SettingsServices | None = None,
                 on_close: Callable[[], None] | None = None,
                 start_at_login: sm.StartAtLogin | None = None,
                 on_usage_details: Callable[[], None] | None = None) -> None:
        self.model = model
        self.services = services or sm.SettingsServices()
        self.login = start_at_login or sm.StartAtLogin(mac=True)
        self._on_close = on_close
        self._usage_details = on_usage_details
        self._errors: dict[str, str] = {}
        self._backends: dict[str, Any] | None = None
        self._keys: dict[str, bool] | None = None
        self._usage: dict[str, Any] | None = None
        self._live: dict[str, str] = {}
        self._login_state: Any = "unknown"
        self._doctor: list[Any] | None = None
        self._asked = False
        self._targets: dict[str, Any] = {}
        self._win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H), _STYLE, NSBackingStoreBuffered, False)
        self._win.setTitle_(sm.TITLE)
        self._win.setReleasedWhenClosed_(False)
        self._tabs = NSTabView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))
        self._tabs.setAutoresizingMask_(18)
        self._items: dict[str, Any] = {}
        for key, title in sm.SECTIONS:
            item = NSTabViewItem.alloc().initWithIdentifier_(key)
            item.setLabel_(title)
            self._tabs.addTabViewItem_(item)
            self._items[key] = item
        self._win.setContentView_(self._tabs)

    # -- opening -----------------------------------------------------------------------
    def show(self, section: str | None = None) -> None:
        self.model.refresh()
        for key in self._items:
            self._draw(key)
        if section in self._items:
            self._tabs.selectTabViewItem_(self._items[section])
        self._fetch()
        self._win.center()
        NSApp().activateIgnoringOtherApps_(True)
        self._win.makeKeyAndOrderFront_(None)

    def _bg(self, work: Callable[[], Any], done: Callable[[Any], None]) -> None:
        def job() -> None:
            try:
                result = work()
            except Exception:  # noqa: BLE001
                result = None
            AppHelper.callAfter(done, result)

        threading.Thread(target=job, daemon=True, name="glance-settings").start()

    def _fetch(self) -> None:
        if not self._asked:
            self._asked = True
            self._bg(lambda: self.services.backends(False), self._got_backends)
        self._bg(self.services.usage, lambda r: self._got("_usage", r, "ai"))
        self._bg(self.services.status, lambda r: self._got(
            "_live", sm.parse_live_problems((r or {}).get("hotkeys_problems")), "shortcuts"))
        self._bg(self.login.get, lambda r: self._got("_login_state", r, "general"))

    def _got(self, attr: str, value: Any, key: str) -> None:
        setattr(self, attr, value)
        self._draw(key)

    def _got_backends(self, reply: Any) -> None:
        self._backends = reply if reply and reply.get("ok") else None
        self._bg(self.services.keys, lambda r: self._got(
            "_keys", (r or {}).get("keys") if r and r.get("ok") else None, "ai"))

    def _draw(self, key: str) -> None:
        target = Target.alloc().init()
        self._targets[key] = target  # the controls' callbacks live as long as the page
        page = _Page(W - 40, target)
        page.heading(dict(sm.SECTIONS)[key], 16.0)
        if self.model.load_error:
            page.text(self.model.load_error, muted=False)
        getattr(self, f"_page_{key}")(page)
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, W - 20, H - 60))
        scroll.setHasVerticalScroller_(True)
        scroll.setDrawsBackground_(False)
        scroll.setAutoresizingMask_(18)
        scroll.setDocumentView_(page.finish())
        self._items[key].setView_(scroll)

    def _apply(self, key: str, error_id: str, error: str | None) -> None:
        if error:
            self._errors[error_id] = error
        else:
            self._errors.pop(error_id, None)
        self._draw(key)

    def _field(self, page: _Page, section: str, f: sm.Field) -> None:
        m = self.model
        shown = sm.display(f, m.value(f.key))

        def done(raw: Any) -> None:
            self._apply(section, f.id, m.set(f, raw))

        if f.kind == "switch":
            control = page.checkbox(bool(shown), done)
        elif f.kind == "choice":
            control = page.popup(f.choices, shown, done)
        else:
            placeholder = {"money": "No limit", "model": "Default"}.get(f.kind, "")
            control = page.field(str(shown), done, placeholder)
        controls = [control]
        if f.kind == "folder":
            controls.append(page.button("Choose…", lambda: self._choose_folder(done)))
        page.row(f.label, f.help, controls, self._errors.get(f.id))

    def _choose_folder(self, done: Callable[[str], None]) -> None:
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseDirectories_(True)
        panel.setCanChooseFiles_(False)
        if panel.runModal() == 1 and panel.URLs():
            path = str(panel.URLs()[0].path())
            home = os.path.expanduser("~")
            if path == home or path.startswith(home + os.sep):
                path = "~" + path[len(home):]
            done(path)

    # -- the sections ------------------------------------------------------------------
    def _page_general(self, page: _Page) -> None:
        for f in sm.GENERAL:
            self._field(page, "general", f)
        state = self._login_state
        box = page.checkbox(state is True, lambda on: self._bg(
            lambda: self.login.set(on), lambda ok: self._got(
                "_login_state", on if ok else (not on), "general")))
        box.setEnabled_(state in (True, False))
        page.row("Start Glance at login", "Runs Glance when you log in (its LaunchAgent).",
                 [box], None if state in (True, False, "unknown") else
                 "Glance is not installed as a login item on this Mac.")

    def _page_features(self, page: _Page) -> None:
        m = self.model
        for feature, on, fields in m.features():
            page.row(feature.label, feature.note, [page.checkbox(on, lambda v, n=feature.name:
                     self._apply("features", n, m.set_feature(n, v)))],
                     self._errors.get(feature.name))
            if on:
                for f in fields:
                    self._field(page, "features", f)

    def _page_ai(self, page: _Page) -> None:
        m = self.model
        info = self._backends
        if info is None and self._asked:
            page.text("Glance is not running, so the AI tools cannot be checked.", False)
        page.heading("AI backends")
        states = (info or {}).get("backends") or {}
        choice = (info or {}).get("choice") if info else m.cfg.backend.choice
        for name in ORDER:
            controls: list[Any] = []
            if name in sm.MODEL_BACKENDS:
                f = sm.model_field(name)
                controls.append(page.field(m.value(f.key) if m.is_set(f.key) else "",
                                           lambda t, f=f: self._apply("ai", f.id, m.set(f, t)),
                                           str(m.value(f.key) or "Default")))
            if name == choice:
                use = page.button("In use", lambda: None)
                use.setEnabled_(False)
            else:
                use = page.button("Use", lambda n=name: self._bg(
                    lambda: self.services.use_backend(n), lambda _r: self._refresh_backends()))
            controls.append(use)
            page.row(TITLES[name], sm.backend_status(states.get(name)) if info else
                     "Checking…", controls, self._errors.get(f"backend.models.{name}"))
        page.heading("API key")
        self._field(page, "ai", sm.API_PROVIDER)
        self._field(page, "ai", sm.API_MODEL)
        provider = m.cfg.backend.api_provider
        saved = bool((self._keys or {}).get(provider))
        key_field = page.field("", lambda _t: None, "Paste the key", secret=True)

        def save() -> None:
            key = str(key_field.stringValue()).strip()
            key_field.setStringValue_("")
            if key:
                self._bg(lambda: self.services.set_key(provider, key),
                         lambda _r: self._refresh_backends())

        controls = [key_field, page.button("Save", save)]
        if saved:
            controls.append(page.button("Remove", lambda: self._bg(
                lambda: self.services.remove_key(provider), lambda _r: self._refresh_backends())))
        page.row("Key", "Saved in the Keychain." if saved else
                 "Goes to the Keychain; never shown again.", controls)
        page.heading("Spending")
        self._field(page, "ai", sm.COST_LIMIT)
        s = sm.usage_summary(self._usage)
        page.row("AI usage", "\n".join(x for x in (s.today, s.month, s.limit) if x),
                 [page.button("Details…", self._details)])

    def _refresh_backends(self) -> None:
        self._asked = True
        self._bg(lambda: self.services.backends(False), self._got_backends)

    def _details(self) -> None:
        if self._usage_details is not None:
            self._usage_details()

    def _page_shortcuts(self, page: _Page) -> None:
        m = self.model
        page.text("Type a shortcut in Glance's spelling: Super is ⌘, Alt ⌥, Ctrl ⌃ "
                  "(e.g. Super+Alt+g). It applies at once.")
        for r in m.shortcuts(self._live):
            field = page.field(r.chord, lambda t, n=r.name: self._apply(
                "shortcuts", n, m.set_shortcut(n, t.strip())))
            reset = page.button("Reset", lambda n=r.name: self._apply(
                "shortcuts", n, m.reset_shortcut(n)))
            reset.setEnabled_(not r.is_default)
            page.row(r.label, "", [field, reset], self._errors.get(r.name) or r.problem)

    def _list(self, page: _Page, section: str, name: str, help_text: str) -> None:
        m = self.model
        items = m.list_items(name)
        builtin = [i.value for i in items if i.builtin]
        if builtin:
            page.text("Built in: " + ", ".join(builtin))
        for item in items:
            if not item.builtin:
                page.row(item.value, "", [page.button("Remove", lambda v=item.value:
                         self._apply(section, name, m.remove_from_list(name, v)))])
        entry = page.field("", lambda t: self._apply(section, name, m.add_to_list(name, t)))
        page.row("Add", help_text, [entry], self._errors.get(name))

    def _page_apps(self, page: _Page) -> None:
        m = self.model
        page.heading("Never in these apps")
        self._list(page, "apps", "app_denylist", "Part of an app's name; Return adds it.")
        page.heading("Per-app settings")
        profiles = (("", "Audience: automatic"), *m.profiles())
        for app in m.apps():
            page.row(app.wm_class, "Glance checks here" if app.capture else "Glance is off here",
                     [page.checkbox(app.capture, lambda on, a=app.wm_class: self._apply(
                         "apps", a, m.set_app_capture(a, on))),
                      page.popup(profiles, app.profile or "", lambda v, a=app.wm_class:
                                 self._apply("apps", a, m.set_app_profile(a, v or None))),
                      page.button("Remove", lambda a=app.wm_class: self._apply(
                          "apps", a, m.remove_app(a)))], self._errors.get(app.wm_class))
        page.row("Add an app", "Its bundle id or name, e.g. com.tinyspeck.slackmacgap",
                 [page.field("", lambda t: self._apply("apps", "add_app", m.add_app(t)))],
                 self._errors.get("add_app"))

    def _page_privacy(self, page: _Page) -> None:
        page.heading("What Glance never does")
        for line in sm.PRIVACY_TEXT:
            page.text("•  " + line, muted=False)
        page.heading("Pages Glance never reads")
        self._list(page, "privacy", "url_denylist", "A regular expression; Return adds it.")
        page.heading("Programs allowed in terminals")
        self._list(page, "privacy", "terminal_allowlist", "e.g. claude")
        page.text(f"Everything here is stored in {self.model.path}.")

    def _page_about(self, page: _Page) -> None:
        page.text(f"Glance {sm.glance_version()}", muted=False)
        page.row("Source code and updates", sm.GITHUB_URL,
                 [page.button("Open GitHub", lambda: _open(sm.GITHUB_URL))])
        page.row("Report a problem", "Opens the issue list on GitHub.",
                 [page.button("Report…", lambda: _open(sm.ISSUES_URL))])
        summary = ("Not run yet." if self._doctor is None else sm.doctor_summary(self._doctor))
        page.row("Setup checks", summary, [page.button("Run checks", self._run_doctor)])
        mark = {"ok": "✓", "warn": "!", "fail": "✗"}
        for r in self._doctor or ():
            page.text(f"{mark.get(r.status, '?')} {r.title} — {r.detail}", muted=False)

    def _run_doctor(self) -> None:
        self._bg(self.services.doctor, lambda r: self._got("_doctor", list(r or []), "about"))


def _open(url: str) -> None:
    NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url))


class _QuitWithWindow(NSObject):
    """On its own (not in glanced), closing the window ends the process."""

    def applicationShouldTerminateAfterLastWindowClosed_(self, _app):
        return True


def main(section: str | None = None) -> int:
    """`glance settings` while glanced is not running: the window in this process."""
    from glance.audience.overrides import Overrides
    from glance.paths import config_path, overrides_path

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(0)  # regular: a Dock icon while the window is open
    delegate = _QuitWithWindow.alloc().init()
    app.setDelegate_(delegate)
    model = sm.SettingsModel(config_path(), mac=True, overrides=Overrides(overrides_path()))
    win = SettingsWindow(model)
    win.show(section)
    AppHelper.runEventLoop()
    return 0
