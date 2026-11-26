"""MacTray: the TrayPort as an NSStatusItem with the Linux tray's menu.

Menu: status line, "Update available — Update now", AI backend ▸, AI usage ▸, Features ▸
("Check: on shortcut / while typing", then the feature switches), "Show suggestions
automatically", "Keyboard shortcuts…",
Pause/Resume, Quit Glance. The menu bar shows the brand mark (a template image). Main thread
only. Needs PyObjC.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from AppKit import NSImage, NSImageRep, NSMenu, NSMenuItem, NSStatusBar, NSVariableStatusItemLength

from glance.backends.base import SIGN_IN
from glance.platform.macos.appkit_util import Target
from glance.ui.onboarding_model import ORDER, TITLES, BackendRow, headline

_STATES = {
    "active": ("✦", "Glance is on"),
    "paused": ("⏸", "Glance is paused"),
    "private": ("✦", "Private field: nothing is read"),
    "degraded": ("⚠", "Glance is restarting a helper"),
    "no_ai": ("✦", "Connect an AI"),
}
INSTALL_HINT = ""
# The brand mark as a flat menu bar icon (packaging/icons/symbolic in the checkout).
_BRAND = Path(__file__).resolve().parents[4] / "packaging" / "icons" / "symbolic"


def _brand_image() -> object | None:
    """The mark as a template image (macOS tints it for the menu bar), or None."""
    img = NSImage.alloc().initWithSize_((18, 18))
    for px in (22, 48):
        rep = NSImageRep.imageRepWithContentsOfFile_(str(_BRAND / f"glance-symbolic-black-{px}.png"))
        if rep is None:
            return None
        rep.setSize_((18, 18))
        img.addRepresentation_(rep)
    img.setTemplate_(True)
    return img


class MacTray:
    def __init__(self, on_pause: Callable[[], None], on_resume: Callable[[], None],
                 on_quit: Callable[[], None],
                 on_choose: Callable[[str], None] = lambda name: None,
                 on_setup: Callable[[], None] = lambda: None,
                 on_auto_suggest: Callable[[bool], None] = lambda on: None,
                 auto_suggest: bool = False,
                 on_update: Callable[[], None] = lambda: None,
                 on_usage_open: Callable[[], None] = lambda: None,
                 on_usage_details: Callable[[], None] = lambda: None,
                 on_check_mode: Callable[[str], None] = lambda mode: None,
                 check_mode: str = "shortcut", check_hint: str = "",
                 on_shortcuts: Callable[[], None] = lambda: None,
                 on_feature: Callable[[str, bool], None] = lambda name, on: None,
                 features: tuple[object, ...] = (),
                 on_shot: Callable[[], None] | None = None,
                 on_settings: Callable[[], None] = lambda: None) -> None:
        from glance.ui.tray_gtk import (
            FEATURES_LABEL,
            SETTINGS_LABEL,
            SHORTCUTS_LABEL,
            SHOT_LABEL,
            UPDATE_LABEL,
            about_label,
            check_mode_labels,
        )

        self._on_feature = on_feature
        self._feature_items: dict[str, object] = {}
        self._feature_on: dict[str, bool] = {}
        self._on_pause, self._on_resume = on_pause, on_resume
        self._on_check_mode = on_check_mode
        self._on_choose, self._on_setup = on_choose, on_setup
        self._on_auto = on_auto_suggest
        self._on_usage_details = on_usage_details
        self._paused = False
        self._state = "active"
        self._ai_line: str | None = None
        self._auto_on = auto_suggest
        self._t = Target.alloc().init()
        self._t.on_menu_open = on_usage_open
        self._item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength)
        self._brand = _brand_image() if _BRAND.is_dir() else None
        self._item.button().setTitle_(_STATES["active"][0])
        self._item.button().setToolTip_("Glance")
        self._menu = NSMenu.alloc().initWithTitle_("Glance")
        self._menu.setAutoenablesItems_(False)
        self._menu.setDelegate_(self._t)
        self._headline = self._add(self._menu, _STATES["active"][1], None)
        self._headline.setEnabled_(False)
        self._update = self._add(self._menu, UPDATE_LABEL, on_update)
        self._update.setHidden_(True)
        self._on_shot = on_shot
        self._shot = self._add(self._menu, SHOT_LABEL, lambda: self._on_shot and self._on_shot())
        self._shot.setHidden_(on_shot is None)
        self._menu.addItem_(NSMenuItem.separatorItem())
        self._backends_item = self._add(self._menu, "AI backend", None)
        self._backends = NSMenu.alloc().initWithTitle_("AI backend")
        self._backends.setAutoenablesItems_(False)
        self._backends_item.setSubmenu_(self._backends)
        self._usage_item = self._add(self._menu, "AI usage", None)
        self._usage = NSMenu.alloc().initWithTitle_("AI usage")
        self._usage.setAutoenablesItems_(False)
        self._usage_item.setSubmenu_(self._usage)
        self._menu.addItem_(NSMenuItem.separatorItem())
        self._features_item = self._add(self._menu, FEATURES_LABEL, None)
        self._features = NSMenu.alloc().initWithTitle_(FEATURES_LABEL)
        self._features.setAutoenablesItems_(False)
        self._features_item.setSubmenu_(self._features)  # type: ignore[attr-defined]
        self._check_items = {
            mode: self._add(self._features, label, lambda m=mode: self._check_clicked(m))
            for mode, label in check_mode_labels(check_hint)}
        self.set_check_mode(check_mode)
        self._features.addItem_(NSMenuItem.separatorItem())
        for row in features:
            name = row.name  # type: ignore[attr-defined]
            self._feature_items[name] = self._add(
                self._features, row.label,  # type: ignore[attr-defined]
                lambda n=name: self._feature_clicked(n))
        self.set_features(features)
        self._auto = self._add(self._menu, "Show suggestions automatically", self._auto_clicked)
        self._auto.setState_(1 if auto_suggest else 0)
        self._add(self._menu, SHORTCUTS_LABEL, on_shortcuts)
        self._add(self._menu, SETTINGS_LABEL, on_settings)
        self._toggle = self._add(self._menu, "Pause", self._toggle_clicked)
        self._menu.addItem_(NSMenuItem.separatorItem())
        self._add(self._menu, about_label(), None).setEnabled_(False)
        self._add(self._menu, "Quit Glance", on_quit)
        self._item.setMenu_(self._menu)
        self.set_backends(())
        self._ai_line = None
        self._show_state()
        self.set_usage(())

    def _add(self, menu: object, title: str, fn: Callable[[], None] | None) -> object:
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            title, "fire:" if fn else None, "")
        if fn is not None:
            item.setTarget_(self._t)
            item.setTag_(self._t.add(fn))
        menu.addItem_(item)  # type: ignore[attr-defined]
        return item

    @property
    def available(self) -> bool:
        return True

    def set_shot(self, on_shot: Callable[[], None] | None) -> None:
        self._on_shot = on_shot
        self._shot.setHidden_(on_shot is None)

    def set_usage(self, lines: tuple[str, ...]) -> None:
        self._usage.removeAllItems()
        for line in lines or ("No usage yet",):
            item = self._add(self._usage, line, None)
            item.setEnabled_(False)  # type: ignore[attr-defined]
        self._usage.addItem_(NSMenuItem.separatorItem())
        self._add(self._usage, "Details…", self._on_usage_details)

    def set_update_available(self, available: bool, label: str | None = None) -> None:
        from glance.ui.tray_gtk import UPDATE_LABEL

        self._update.setTitle_(label or UPDATE_LABEL)
        self._update.setHidden_(not available)

    def set_state(self, state: str) -> None:
        self._state = state if state in _STATES else "active"
        self._paused = state == "paused"
        self._show_state()

    def set_backends(self, rows: tuple[BackendRow, ...]) -> None:
        self._backends.removeAllItems()
        ready = [r for r in rows if r.state == "ready"]
        for row in ready:
            item = self._add(self._backends, row.title, lambda n=row.name: self._on_choose(n))
            item.setState_(1 if row.selected else 0)  # type: ignore[attr-defined]
        if not ready:
            self._add(self._backends, "No AI connected. Sign in to one:", None).setEnabled_(False)  # type: ignore[attr-defined]
            for name in ORDER:
                self._add(self._backends, f"{TITLES[name]}: {SIGN_IN[name]}", None).setEnabled_(False)  # type: ignore[attr-defined]
        self._backends.addItem_(NSMenuItem.separatorItem())
        self._add(self._backends, "Set up…" if ready else "Connect an AI…", self._on_setup)
        self._ai_line = headline(rows)
        self._show_state()

    def set_auto_suggest(self, on: bool) -> None:
        self._auto_on = on
        self._auto.setState_(1 if on else 0)

    def set_check_mode(self, mode: str) -> None:
        for m, item in self._check_items.items():
            item.setState_(1 if m == mode else 0)  # type: ignore[attr-defined]

    def set_features(self, rows: tuple[object, ...]) -> None:
        for row in rows:
            item = self._feature_items.get(row.name)  # type: ignore[attr-defined]
            if item is not None:
                self._feature_on[row.name] = bool(row.on)  # type: ignore[attr-defined]
                item.setState_(1 if row.on else 0)  # type: ignore[attr-defined]

    def _feature_clicked(self, name: str) -> None:
        on = not self._feature_on.get(name, False)
        self._feature_on[name] = on
        self._feature_items[name].setState_(1 if on else 0)  # type: ignore[attr-defined]
        self._on_feature(name, on)

    def _check_clicked(self, mode: str) -> None:
        self.set_check_mode(mode)
        self._on_check_mode(mode)

    def _show_state(self) -> None:
        icon, line = _STATES[self._state]
        if self._state == "active" and self._ai_line is not None:
            line = self._ai_line
        button = self._item.button()
        if self._brand is not None and icon == "✦":  # the mark, not the sparkle
            button.setImage_(self._brand)
            button.setTitle_("")
        else:
            button.setImage_(None)
            button.setTitle_(icon)
        self._headline.setTitle_(line)
        self._toggle.setTitle_("Resume" if self._paused else "Pause")

    def _auto_clicked(self) -> None:
        self.set_auto_suggest(not self._auto_on)
        self._on_auto(self._auto_on)

    def _toggle_clicked(self) -> None:
        if self._paused:
            self._on_resume()
        else:
            self._on_pause()
