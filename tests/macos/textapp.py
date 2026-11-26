"""A tiny AppKit test app for the macOS acceptance tests: one window with a multi-line text
view, a one-line text field and a secure (password) field.

It is driven over stdin/stdout, one JSON object per line, never by keystrokes from the test:

    {"cmd": "activate"}                     bring this app to the front
    {"cmd": "focus", "what": "view"|"field"|"secure"}
    {"cmd": "set", "what": ..., "text": ..., "caret": n}   set the text, caret at n
    {"cmd": "select", "start": a, "end": b}  select in the text view
    {"cmd": "get", "what": ...}              -> {"text": ...}
    {"cmd": "frame"}                         -> {"frame": [x, y, w, h]} (AppKit coordinates)
    {"cmd": "move", "dx": .., "dy": ..}      move the window (AppKit: +dy is up)
    {"cmd": "pid"}                           -> {"pid": ...}
    {"cmd": "quit"}

Every reply has "ok". With `--sock PATH` the same lines go over a unix socket instead (the
tests launch it as a small .app through LaunchServices, which macOS requires before it lets
an app take the focus). Synthetic test text only.
"""
from __future__ import annotations

import json
import os
import sys
import threading

import objc
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyRegular,
    NSBackingStoreBuffered,
    NSFont,
    NSScrollView,
    NSSecureTextField,
    NSTextField,
    NSTextView,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSMakeRange, NSMakeRect, NSObject


class Driver(NSObject):
    def initWithParts_(self, parts):
        self = objc.super(Driver, self).init()  # noqa: PLW0642
        if self is None:
            return None
        self.parts = parts
        self.out = sys.stdout
        return self

    def run_(self, line):
        try:
            reply = handle(self.parts, json.loads(str(line)))
        except Exception as e:  # noqa: BLE001
            reply = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        self.out.write(json.dumps(reply) + "\n")
        self.out.flush()


def handle(p: dict, msg: dict) -> dict:
    app, win = p["app"], p["win"]
    cmd = msg["cmd"]
    widgets = {"view": p["view"], "field": p["field"], "secure": p["secure"]}
    if cmd == "activate":
        app.activateIgnoringOtherApps_(True)
        win.makeKeyAndOrderFront_(None)
        return {"ok": True}
    if cmd == "focus":
        w = widgets[msg["what"]]
        win.makeFirstResponder_(w)
        return {"ok": True}
    if cmd == "set":
        w, text = widgets[msg["what"]], msg["text"]
        caret = int(msg.get("caret", len(text)))
        if msg["what"] == "view":
            w.setString_(text)
            w.setSelectedRange_(NSMakeRange(caret, 0))
        else:
            w.setStringValue_(text)
            editor = w.currentEditor()
            if editor is not None:
                editor.setSelectedRange_(NSMakeRange(caret, 0))
        return {"ok": True}
    if cmd == "select":
        p["view"].setSelectedRange_(NSMakeRange(int(msg["start"]),
                                                int(msg["end"]) - int(msg["start"])))
        return {"ok": True}
    if cmd == "get":
        w = widgets[msg["what"]]
        text = str(w.string()) if msg["what"] == "view" else str(w.stringValue())
        return {"ok": True, "text": text}
    if cmd == "frame":
        f = win.frame()
        return {"ok": True, "frame": [f.origin.x, f.origin.y, f.size.width, f.size.height]}
    if cmd == "move":
        f = win.frame()
        win.setFrameOrigin_((f.origin.x + float(msg["dx"]), f.origin.y + float(msg["dy"])))
        return {"ok": True}
    if cmd == "pid":
        return {"ok": True, "pid": os.getpid()}
    if cmd == "quit":  # after the reply is out
        app.performSelector_withObject_afterDelay_("terminate:", None, 0.05)
        return {"ok": True}
    return {"ok": False, "error": "unknown command"}


class EditView(NSTextView):
    """Takes ⌘V/⌘Z/⌘A itself (before the menu): no menu-title flash, whose tracking loop keeps
    the app from answering AX for a while (see the report)."""

    def performKeyEquivalent_(self, event):
        if self.window() is None or self.window().firstResponder() is not self:
            return objc.super(EditView, self).performKeyEquivalent_(event)
        flags = int(event.modifierFlags()) & 0x1F0000
        key = str(event.charactersIgnoringModifiers() or "").lower()
        if flags == 0x100000 and key in ("v", "z", "a", "c", "x"):
            {"v": self.paste_, "a": self.selectAll_, "c": self.copy_, "x": self.cut_,
             "z": lambda s: self.undoManager().undo()}[key](None)
            return True
        if flags == 0x120000 and key == "z":
            self.undoManager().redo()
            return True
        return objc.super(EditView, self).performKeyEquivalent_(event)


def edit_menu():
    from AppKit import NSMenu, NSMenuItem

    bar = NSMenu.alloc().init()
    top = NSMenuItem.alloc().init()
    bar.addItem_(top)
    edit = NSMenu.alloc().initWithTitle_("Edit")
    for title, action, key in (("Undo", "undo:", "z"), ("Redo", "redo:", "Z"),
                               ("Cut", "cut:", "x"), ("Copy", "copy:", "c"),
                               ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")):
        edit.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key))
    top.setSubmenu_(edit)
    return bar


def main() -> int:
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    app.setMainMenu_(edit_menu())  # ⌘V and ⌘Z are menu key equivalents, as in every real app
    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(300, 300, 560, 300), NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
        NSBackingStoreBuffered, False)
    win.setTitle_("Glance test app")
    content = win.contentView()
    scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(20, 110, 520, 170))
    view = EditView.alloc().initWithFrame_(NSMakeRect(0, 0, 520, 170))
    view.setFont_(NSFont.systemFontOfSize_(14))
    view.setAutomaticSpellingCorrectionEnabled_(False)
    view.setAutomaticQuoteSubstitutionEnabled_(False)
    view.setContinuousSpellCheckingEnabled_(False)
    view.setAllowsUndo_(True)
    scroll.setDocumentView_(view)
    content.addSubview_(scroll)
    field = NSTextField.alloc().initWithFrame_(NSMakeRect(20, 64, 520, 26))
    content.addSubview_(field)
    secure = NSSecureTextField.alloc().initWithFrame_(NSMakeRect(20, 22, 520, 26))
    content.addSubview_(secure)
    win.makeKeyAndOrderFront_(None)
    win.makeFirstResponder_(view)
    app.activateIgnoringOtherApps_(True)
    parts = {"app": app, "win": win, "view": view, "field": field, "secure": secure}
    driver = Driver.alloc().initWithParts_(parts)

    # Driven over stdin/stdout, or (launched through LaunchServices by `open`, which has no
    # pipes to give) over a unix socket named by --sock.
    sock_path = sys.argv[sys.argv.index("--sock") + 1] if "--sock" in sys.argv else None
    if sock_path:
        import socket

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if os.path.exists(sock_path):
            os.unlink(sock_path)
        server.bind(sock_path)
        server.listen(1)

    def read() -> None:
        if sock_path:
            conn, _ = server.accept()
            inp = conn.makefile("r", encoding="utf-8")
            driver.out = conn.makefile("w", encoding="utf-8")
        else:
            inp = sys.stdin
        driver.out.write(json.dumps({"ok": True, "ready": True, "pid": os.getpid()}) + "\n")
        driver.out.flush()
        for line in inp:
            if line.strip():
                driver.performSelectorOnMainThread_withObject_waitUntilDone_("run:", line, False)
        driver.performSelectorOnMainThread_withObject_waitUntilDone_(
            "run:", '{"cmd": "quit"}', False)

    threading.Thread(target=read, daemon=True).start()
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
