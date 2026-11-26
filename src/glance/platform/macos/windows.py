"""The two small windows the Linux build draws in GTK, for macOS: the usage report (a
read-only text panel) and the "Connect an AI" stand-in (a notification pointing at the tray
menu and the CLI; the full onboarding window is not ported yet). Needs PyObjC.
"""
from __future__ import annotations

from typing import Any

from AppKit import (
    NSApp,
    NSBackingStoreBuffered,
    NSFont,
    NSPanel,
    NSScrollView,
    NSTextView,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSMakeRect

SETUP_TITLE = "Glance: connect an AI"
SETUP_BODY = ("Choose one from the menu bar (✦ → AI backend), or in Terminal: "
              "glance backend list, then glance backend use <name>.")


class UsageWindow:
    def __init__(self) -> None:
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable
        self._win = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(200, 200, 520, 360), style, NSBackingStoreBuffered, False)
        self._win.setTitle_("Glance AI usage")
        self._win.setReleasedWhenClosed_(False)
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, 520, 360))
        scroll.setHasVerticalScroller_(True)
        scroll.setAutoresizingMask_(18)
        self._text = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, 520, 360))
        self._text.setEditable_(False)
        self._text.setFont_(NSFont.monospacedSystemFontOfSize_weight_(12, 0))
        self._text.setAutoresizingMask_(18)
        scroll.setDocumentView_(self._text)
        self._win.setContentView_(scroll)

    def show(self, text: str) -> None:
        self._text.setString_(text)
        NSApp().activateIgnoringOtherApps_(True)
        self._win.makeKeyAndOrderFront_(None)


class SetupNotice:
    """Stands in for the onboarding window: `show()` says where to choose an AI."""

    def __init__(self, notifier: Any, **_kw: Any) -> None:
        self._notifier = notifier

    def show(self) -> None:
        self._notifier.notify(SETUP_TITLE, SETUP_BODY)
