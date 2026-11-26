"""Small AppKit helpers shared by the macOS overlay, ghost and tray: panels, colours, the
button target, the theme. Needs PyObjC (AppKit); imported only on a Mac.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import objc
from AppKit import (
    NSApp,
    NSAppearanceNameAqua,
    NSAppearanceNameDarkAqua,
    NSBackingStoreBuffered,
    NSColor,
    NSPanel,
    NSScreen,
    NSStatusWindowLevel,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorIgnoresCycle,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSMakeRect, NSObject

from glance.log import log_event
from glance.platform.macos.geometry import from_cocoa, to_cocoa
from glance.protocol import Rect
from glance.ui.theme import DARK, LIGHT


class GlancePanel(NSPanel):
    """A borderless, non-activating panel on every Space, above normal windows. It becomes
    key only when `allow_key` is set (the "Rewrite as…" field)."""

    def canBecomeKeyWindow(self):
        return bool(getattr(self, "allow_key", False))

    def canBecomeMainWindow(self):
        return False

    def cancelOperation_(self, _sender):
        cb = getattr(self, "on_cancel", None)
        if cb is not None:
            cb()


def make_panel(click_through: bool = False, can_key: bool = False) -> Any:
    style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
    p = GlancePanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, 10, 10), style, NSBackingStoreBuffered, False)
    p.allow_key = can_key
    p.setLevel_(NSStatusWindowLevel)
    p.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces
                             | NSWindowCollectionBehaviorFullScreenAuxiliary
                             | NSWindowCollectionBehaviorStationary
                             | NSWindowCollectionBehaviorIgnoresCycle)
    p.setHidesOnDeactivate_(False)
    p.setBecomesKeyOnlyIfNeeded_(True)
    p.setFloatingPanel_(True)
    p.setOpaque_(False)
    p.setBackgroundColor_(NSColor.clearColor())
    p.setHasShadow_(True)
    p.setIgnoresMouseEvents_(click_through)
    p.setReleasedWhenClosed_(False)
    p.setAnimationBehavior_(2)  # NSWindowAnimationBehaviorNone: no zoom-in per show
    return p


def primary_height() -> float:
    screens = NSScreen.screens() or ()
    return float(screens[0].frame().size.height) if screens else 0.0


def place(panel: Any, r: Rect) -> None:
    """Put `panel` at top-left `Rect` `r`."""
    x, y, w, h = to_cocoa(r, primary_height())
    panel.setFrame_display_(NSMakeRect(x, y, w, h), True)


def frame_of(panel: Any) -> tuple[int, int, int, int]:
    f = panel.frame()
    r = from_cocoa(f.origin.x, f.origin.y, f.size.width, f.size.height, primary_height())
    return r.x, r.y, r.w, r.h


def monitors(visible: bool = True) -> list[Rect]:
    """Every screen in top-left space; `visible` leaves out the menu bar and the Dock."""
    h = primary_height()
    out = []
    for s in NSScreen.screens() or ():
        f = s.visibleFrame() if visible else s.frame()
        out.append(from_cocoa(f.origin.x, f.origin.y, f.size.width, f.size.height, h))
    return out


def is_dark() -> bool:
    app = NSApp()
    if app is None:
        return False
    best = app.effectiveAppearance().bestMatchFromAppearancesWithNames_(
        [NSAppearanceNameAqua, NSAppearanceNameDarkAqua])
    return best == NSAppearanceNameDarkAqua


def tokens() -> dict[str, str]:
    return DARK if is_dark() else LIGHT


def color(hex_or_rgba: str, alpha: float = 1.0) -> Any:
    s = hex_or_rgba.strip()
    if s.startswith("rgba"):
        parts = [p.strip() for p in s[s.index("(") + 1:s.index(")")].split(",")]
        r, g, b = (int(p) / 255.0 for p in parts[:3])
        return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, float(parts[3]) * alpha)
    s = s.lstrip("#")
    r, g, b = (int(s[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, alpha)


class Target(NSObject):
    """The one target for every button and menu item: `fire:` runs callbacks[sender.tag]."""

    def init(self):
        self = objc.super(Target, self).init()  # noqa: PLW0642 - PyObjC init idiom
        if self is None:
            return None
        self.callbacks = []
        return self

    @objc.python_method
    def add(self, fn: Callable[[], None]) -> int:
        self.callbacks.append(fn)
        return len(self.callbacks) - 1

    @objc.python_method
    def reset(self) -> None:
        self.callbacks = []

    def fire_(self, sender):
        try:
            fn = self.callbacks[int(sender.tag())]
        except (IndexError, ValueError):
            return
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - a broken handler must not end the app
            log_event("ui_action_error", kind=type(e).__name__)

    def menuWillOpen_(self, _menu):
        cb = getattr(self, "on_menu_open", None)
        if cb is not None:
            try:
                cb()
            except Exception as e:  # noqa: BLE001
                log_event("ui_action_error", kind=type(e).__name__)
