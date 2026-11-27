"""XlibSelectionWatch: "a selection settled" on X11, without reading the selection.

What it listens to, on an X connection of its own, from the main loop:

  * XFixes selection-owner events for PRIMARY: an app took the selection (the user selected
    something) or gave it up. The event names the owner window and nothing else; the text is
    never asked for here.
  * XInput2 raw button events (no grab, nothing is taken from anyone): whether the left
    button is held, and when it was let go. A selection made with the mouse settles
    `SETTLE_MS` after the button is up and the owner stopped changing; a selection made with
    the keyboard (Shift+arrows, Ctrl+A: the user is typing in that field) never shows the
    mark.
  * While the mark is up (`arm(True)`) only: raw key presses, scroll-wheel buttons, clicks
    and the active window changing (`_NET_ACTIVE_WINDOW`), each reported as a kind (`key`,
    `scroll`, `click`, `focus`) without its content, so the mark hides. Keys are not listened
    to at any other time.
"""
from __future__ import annotations

import struct
from collections.abc import Callable
from typing import Any

from Xlib import X, Xatom, display, error
from Xlib.ext import ge, xinput

from glance.log import log_event
from glance.ui.selection_bar import SETTLE_MS

POLL_MS = 80          # while the button is held: is it up yet
RECENT_RELEASE_S = 0.6  # an owner change this soon after a release is the mouse's
_SCROLL = frozenset({4, 5, 6, 7})
_BUTTONS = xinput.RawButtonPressMask | xinput.RawButtonReleaseMask

Later = Callable[[int, Callable[[], None]], Any]


class XlibSelectionWatch:
    def __init__(self, call_later: Later, now: Callable[[], float]) -> None:
        self._later, self._now = call_later, now
        self._d: Any = None
        self._watch = 0
        self._armed = False
        self._held = False
        self._released_at = -1e9
        self._changed_at = 0.0
        self._settling: Any = None
        self._on_settled: Callable[[int, int], None] = lambda x, y: None
        self._on_cleared: Callable[[], None] = lambda: None
        self._on_input: Callable[[str, int, int], None] = lambda kind, x, y: None

    @property
    def running(self) -> bool:
        return self._d is not None

    def start(self, on_settled: Callable[[int, int], None], on_cleared: Callable[[], None],
              on_input: Callable[[str, int, int], None]) -> None:
        if self._d is not None:
            return
        self._on_settled, self._on_cleared, self._on_input = on_settled, on_cleared, on_input
        try:
            d = display.Display()
            if not d.has_extension("XFIXES") or not d.has_extension("XInputExtension"):
                d.close()
                log_event("selection_watch_unavailable")
                return
            d.xfixes_query_version()
            d.xinput_query_version()
            self._xi = d.query_extension("XInputExtension").major_opcode
            self._root = d.screen().root
            self._active = d.intern_atom("_NET_ACTIVE_WINDOW")
            d.xfixes_select_selection_input(
                self._root, Xatom.PRIMARY,
                1 | 2 | 4)  # set-owner, owner window destroyed, owner client closed
            self._root.xinput_select_events([(xinput.AllMasterDevices, _BUTTONS)])
            d.flush()
        except (error.DisplayError, error.XError, OSError) as e:
            log_event("selection_watch_unavailable", kind=type(e).__name__)
            return
        from gi.repository import GLib

        self._d = d
        log_event("selection_watch_started")
        self._watch = GLib.io_add_watch(d.fileno(), GLib.PRIORITY_DEFAULT, GLib.IO_IN,
                                        lambda *_a: self._pump())
        self._pump()

    def stop(self) -> None:
        if self._d is None:
            return
        from gi.repository import GLib

        if self._watch:
            GLib.source_remove(self._watch)
            self._watch = 0
        self._cancel()
        try:
            self._d.close()
        except Exception as e:  # noqa: BLE001 - the server may be gone already
            log_event("selection_watch_close_failed", kind=type(e).__name__)
        self._d = None
        self._armed = False

    def arm(self, on: bool) -> None:
        """Report keys, scrolling, clicks and focus changes (only while the mark is up)."""
        if self._d is None or on == self._armed:
            return
        self._armed = on
        mask = _BUTTONS | (xinput.RawKeyPressMask if on else 0)
        try:
            self._root.xinput_select_events([(xinput.AllMasterDevices, mask)])
            self._root.change_attributes(event_mask=X.PropertyChangeMask if on else 0)
            self._d.flush()
        except error.XError:
            pass

    # ------------------------------------------------------------------ events

    def _pump(self) -> bool:
        d = self._d
        if d is None:
            return False
        try:
            while d.pending_events():
                ev = d.next_event()
                try:
                    self._event(ev)
                except Exception as e:  # noqa: BLE001 - one odd event must not end the watch
                    log_event("selection_watch_event_error", kind=type(e).__name__)
        except (error.ConnectionClosedError, OSError):
            log_event("selection_watch_lost")
            self._d = None
            self._watch = 0
            return False
        return True

    def _event(self, ev: Any) -> None:
        name = type(ev).__name__
        if name == "SetSelectionOwnerNotify" or name.startswith("Selection"):
            owner = getattr(ev, "owner", 0) if name == "SetSelectionOwnerNotify" else 0
            self._owner_changed(int(getattr(owner, "id", owner) or 0))
        elif ev.type == ge.GenericEventCode and getattr(ev, "extension", None) == self._xi:
            self._raw(int(ev.evtype), ev.data)
        elif ev.type == X.PropertyNotify and self._armed and ev.atom == self._active:
            self._input("focus")

    def _raw(self, evtype: int, data: Any) -> None:
        try:
            detail = struct.unpack_from("=I", bytes(data), 6)[0]
        except (struct.error, TypeError):
            return
        if evtype == xinput.RawKeyPress:
            if self._armed:
                self._input("key")
        elif evtype == xinput.RawButtonPress:
            if detail in _SCROLL:
                if self._armed:
                    self._input("scroll")
            elif detail == 1:
                self._held = True
                if self._armed:
                    self._input("click")
        elif evtype == xinput.RawButtonRelease and detail == 1:
            self._held = False
            self._released_at = self._now()

    def _owner_changed(self, owner: int) -> None:
        if not owner:
            self._cancel()
            self._on_cleared()
            return
        now = self._now()
        if not (self._held or now - self._released_at <= RECENT_RELEASE_S):
            # Made with the keyboard: the user is working in that field (the strip's job).
            log_event("selection_not_mouse")
            self._cancel()
            self._on_cleared()
            return
        self._changed_at = now
        if self._settling is None:
            self._settling = self._later(POLL_MS, self._settle)

    def _settle(self) -> None:
        self._settling = None
        if self._d is None:
            return
        quiet_s = self._now() - max(self._changed_at, self._released_at)
        if self._held or quiet_s * 1000 < SETTLE_MS:
            self._settling = self._later(POLL_MS, self._settle)
            return
        try:
            q = self._root.query_pointer()
            x, y = int(q.root_x), int(q.root_y)
        except error.XError:
            return
        self._on_settled(x, y)

    def _input(self, kind: str) -> None:
        try:
            q = self._root.query_pointer()
            x, y = int(q.root_x), int(q.root_y)
        except error.XError:
            x = y = 0
        self._on_input(kind, x, y)

    def _cancel(self) -> None:
        if self._settling is not None:
            self._settling.cancel()
            self._settling = None
