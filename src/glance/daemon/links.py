"""The daemon's links to the outside: the capture socket and the overlay wrappers.

No GTK here: these wrap ports, so they are tested with fakes.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from glance.log import log_event
from glance.protocol import (
    APPLY,
    APPLY_RESULT,
    FOCUS_LOST,
    HELLO,
    PASTE,
    RESET,
    REVERT,
    SNAPSHOT,
    ApplyRequest,
    ApplyResult,
    FieldSnapshot,
)
from glance.ui.hotkeys import HotkeyPort
from glance.ui.ports import OverlayPort

ESCAPE = "Escape"
USE_KEY = "Alt+Return"  # the default of `[ui] use_key`


# ---------------------------------------------------------------------- capture link


class CaptureLink:
    """CapturePort over the capture socket. Messages arrive on reader threads and are
    handed to the main loop through `post`; only the peer that said HELLO last counts."""

    def __init__(self, post: Callable[[Callable[[], None]], None]) -> None:
        self._post = post
        self._conn: Any = None
        self.connected = False
        self.controller: Any = None

    def attach(self, controller: Any) -> None:
        self.controller = controller

    # reader threads

    def on_message(self, conn: Any, msg_type: str, payload: dict[str, Any]) -> None:
        self._post(lambda: self._handle(conn, msg_type, payload))

    def on_disconnect(self, conn: Any) -> None:
        self._post(lambda: self._gone(conn))

    # main loop

    def _handle(self, conn: Any, msg_type: str, payload: dict[str, Any]) -> None:
        if msg_type == HELLO:
            if self._conn is not None and self._conn is not conn:
                self._conn.close()  # a restarted capture: the old link is dead weight
            self._conn = conn
            self.connected = True
            log_event("capture_link_up")
            if self.controller is not None:
                self.controller.handle_capture_connected()
            return
        if conn is not self._conn or self.controller is None:
            return
        try:
            if msg_type == SNAPSHOT:
                s = FieldSnapshot.from_dict(payload)
                if not all(isinstance(v, str) for v in (s.text, s.field_id, s.wm_class)):
                    raise TypeError("snapshot")
                self.controller.handle_snapshot(s)
            elif msg_type == FOCUS_LOST:
                reason = payload.get("reason")
                self.controller.handle_focus_lost(reason if isinstance(reason, str) else "")
            elif msg_type == APPLY_RESULT:
                self.controller.handle_apply_result(ApplyResult.from_dict(payload))
        except (KeyError, TypeError, ValueError):
            log_event("capture_message_malformed", type=msg_type)

    def _gone(self, conn: Any) -> None:
        if conn is not self._conn:
            return
        self._conn = None
        self.connected = False
        log_event("capture_link_down")
        if self.controller is not None:
            self.controller.handle_capture_disconnected()

    # CapturePort

    def _send(self, msg_type: str, payload: dict[str, Any]) -> None:
        if self._conn is None:
            return
        try:
            self._conn.send(msg_type, payload)
        except ConnectionError:
            pass  # the reader thread reports the disconnect

    def send_apply(self, req: ApplyRequest) -> None:
        self._send(APPLY, req.to_dict())

    def send_revert(self) -> None:
        self._send(REVERT, {})

    def send_paste(self, text: str, window: int = 0, wm_class: str = "") -> None:
        """Select-anything's unverified paste; the answer is an APPLY_RESULT "paste"."""
        self._send(PASTE, {"text": text, "window": window, "wm_class": wm_class})

    def send_reset(self) -> None:
        """After suspend/resume: capture re-registers its AT-SPI listeners and resends."""
        self._send(RESET, {})


# ---------------------------------------------------------------------- escape


class PanelEscape:
    """An OverlayPort that holds the Escape grab exactly while the drawer, or a strip the user
    opened (`StripView.on_demand`), is on screen, and the use key (Alt+Return; `use_key()`
    says which) exactly while the strip offers a fix, the same transient rule as
    autocomplete's Tab. Plain Return is never taken."""

    def __init__(self, overlay: OverlayPort, hotkeys: HotkeyPort,
                 on_escape: Callable[[], None],
                 on_use: Callable[[], None] | None = None,
                 use_key: Callable[[], str] = lambda: USE_KEY) -> None:
        self._overlay = overlay
        self._hotkeys = hotkeys
        self._on_escape = on_escape
        self._on_use = on_use
        self._use_key = use_key
        self.panel_visible = False
        self._strip_open = False  # a strip the user opened is up: Esc closes it
        self._esc_wanted = False
        self._grabbed = False
        self._use_grabbed = False

    def show_strip(self, view: Any, anchor: Any) -> None:
        self._overlay.show_strip(view, anchor)
        self._strip_open = bool(getattr(view, "on_demand", False))
        self._sync_escape()
        if not (view.can_use and self._on_use is not None):
            self._release_use()
        elif not self._use_grabbed:
            try:
                self._use_grabbed = self._hotkeys.grab("strip_use", self._use_key(),
                                                       self._on_use)
            except ValueError:  # not a key on this keyboard: the Use button remains
                self._use_grabbed = False

    def hide_strip(self) -> None:
        self._overlay.hide_strip()
        self._release_use()
        self._strip_open = False
        self._sync_escape()

    @property
    def use_grabbed(self) -> bool:
        """The use key is Glance's right now."""
        return self._use_grabbed

    @property
    def escape_grabbed(self) -> bool:
        return self._grabbed

    def _release_use(self) -> None:
        if self._use_grabbed:
            self._use_grabbed = False
            self._hotkeys.ungrab("strip_use")

    def _sync_escape(self) -> None:
        """Grab once when Esc becomes wanted, let go when it no longer is."""
        want = self.panel_visible or self._strip_open
        if want == self._esc_wanted:
            return
        self._esc_wanted = want
        if want:
            try:
                self._grabbed = self._hotkeys.grab("escape", ESCAPE, self._on_escape)
            except ValueError:  # no Escape key on this keyboard: the Close button remains
                self._grabbed = False
        elif self._grabbed:
            self._grabbed = False
            self._hotkeys.ungrab("escape")

    def show_panel(self, view: Any, *where: Any, **kw: Any) -> None:
        self._overlay.show_panel(view, *where, **kw)
        self.panel_visible = True
        self._sync_escape()

    def hide_panel(self) -> None:
        self._overlay.hide_panel()
        if self.panel_visible:
            self.panel_visible = False
        self._sync_escape()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._overlay, name)  # the rest of the OverlayPort, unchanged


class FieldPanelOverlay:
    """The overlay as the Controller sees it once select-anything can own the panel.

    While `selection_open()` the Controller's `hide_panel` is dropped: its redraws (one per
    snapshot) must not take away a panel it did not open. A `show_panel` from the Controller
    means the user opened the field's panel, so select-anything is closed first.
    """

    def __init__(self, overlay: Any, selection_open: Callable[[], bool],
                 close_selection: Callable[[], None]) -> None:
        self._overlay = overlay
        self._open = selection_open
        self._close = close_selection

    @property
    def panel_visible(self) -> bool:
        return bool(getattr(self._overlay, "panel_visible", False))

    def show_panel(self, view: Any, *where: Any, **kw: Any) -> None:
        if self._open():
            self._close()
        self._overlay.show_panel(view, *where, **kw)

    def hide_panel(self) -> None:
        if not self._open():
            self._overlay.hide_panel()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._overlay, name)  # the rest of the OverlayPort, unchanged
