"""The "Keyboard shortcuts" window on macOS (the menu bar item's entry): Glance's chords as
keycap rows, as the Linux window shows them. Main thread only. Needs PyObjC."""
from __future__ import annotations

from typing import Any

from AppKit import NSBackingStoreBuffered, NSWindow
from Foundation import NSMakePoint, NSMakeRect

from glance.platform.macos.appkit_util import color, tokens
from glance.platform.macos.overlay import _Card, _Flipped, _keycap, _label, _mark_view

TITLE = "Keyboard shortcuts"
_STYLE = 1 | 2  # titled, closable
_KEYS_W, _WHAT_W, _ROW_H, _PAD = 190.0, 230.0, 30.0, 22.0


class ShortcutsWindow:
    """`show(rows)`: rows of (keycaps, what it does), from `shortcut_table.glance_shortcuts`."""

    def __init__(self) -> None:
        self._win: Any = None

    def show(self, rows: list[tuple[list[str], str]]) -> None:
        t = tokens()
        per = (len(rows) + 1) // 2
        col_w = _KEYS_W + _WHAT_W
        w = 2 * _PAD + 2 * col_w
        h = 2 * _PAD + 46 + per * _ROW_H
        card = _Card.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        card.fill, card.border, card.radius = color(t["surface"]), None, 0.0
        mark = _mark_view(22)
        mark.setFrameOrigin_(NSMakePoint(_PAD, _PAD + 4))
        card.addSubview_(mark)
        title = _label(TITLE, color(t["text"]), 300, bold=True, lines=1, size=14)
        title.setFrameOrigin_(NSMakePoint(_PAD + 32, _PAD))
        card.addSubview_(title)
        sub = _label("Glance works with the keyboard first.", color(t["muted"]), 300, lines=1,
                     size=12)
        sub.setFrameOrigin_(NSMakePoint(_PAD + 32, _PAD + 18))
        card.addSubview_(sub)
        for i, (keys, what) in enumerate(rows):
            col, row = divmod(i, per)
            x, y = _PAD + col * col_w, _PAD + 46 + row * _ROW_H
            caps = _Flipped.alloc().initWithFrame_(NSMakeRect(x, y, _KEYS_W, 22))
            cx = 0.0
            for key in keys:
                cap = _keycap(key, t)
                cap.setFrameOrigin_(NSMakePoint(cx, 1))
                caps.addSubview_(cap)
                cx += cap.frame().size.width + 4
            card.addSubview_(caps)
            label = _label(what, color(t["body"]), _WHAT_W - 10, lines=1)
            label.setFrameOrigin_(NSMakePoint(x + _KEYS_W, y + 2))
            card.addSubview_(label)
        if self._win is None:
            self._win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, w, h), _STYLE, NSBackingStoreBuffered, False)
            self._win.setTitle_(f"Glance — {TITLE}")
            self._win.setReleasedWhenClosed_(False)
        self._win.setContentSize_((w, h))
        self._win.setContentView_(card)
        self._win.center()
        from AppKit import NSApp

        NSApp().activateIgnoringOtherApps_(True)
        self._win.makeKeyAndOrderFront_(None)
