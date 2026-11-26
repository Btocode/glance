"""ShellClipboard: the ClipboardPort over Glance's GNOME Shell extension (Wayland).

The shell's St.Clipboard holds the replacement for one Ctrl+V. Wayland has no way to offer
it to one client only (X11's XlibClipboard does that), so a clipboard manager may see it;
the engine pastes only what the virtual keyboard cannot type. Only a text clipboard can be
saved and put back faithfully: with an image or files on it `save` returns None and the
engine does not paste. The restore waits RESTORE_AFTER_MS in the shell, since the pasting
application reads the clipboard asynchronously after Ctrl+V.
"""
from __future__ import annotations

from glance.log import log_event
from glance.shellbus import ShellBus, ShellUnavailable
from glance.writeback.ports import ClipSnapshot

RESTORE_AFTER_MS = 250
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024


class ShellClipboard:
    def __init__(self, bus: ShellBus) -> None:
        self._bus = bus

    def close(self) -> None:
        pass

    def save(self) -> ClipSnapshot | None:
        try:
            answer = self._bus.call("ClipboardSave", {"which": "clipboard"})
        except ShellUnavailable:
            return None
        if not isinstance(answer, dict) or not answer.get("restorable"):
            return None
        text = answer.get("text")
        if text is not None and not isinstance(text, str):
            return None
        if text is not None and len(text.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            return None
        return ClipSnapshot(text)

    def set_secret_text(self, text: str, target_window: int) -> None:
        answer = self._bus.call("ClipboardSet", {"which": "clipboard", "text": text})
        if not (isinstance(answer, dict) and answer.get("ok")):
            raise RuntimeError("the shell did not take the clipboard")

    def restore(self, snap: ClipSnapshot) -> None:
        text = snap.payload if isinstance(snap.payload, str) else None
        try:
            self._bus.call("ClipboardRestore", {"which": "clipboard", "text": text,
                                                "after_ms": RESTORE_AFTER_MS},
                           timeout_ms=RESTORE_AFTER_MS + 2000)
        except ShellUnavailable:
            log_event("shell_clipboard_restore_failed")
            raise
