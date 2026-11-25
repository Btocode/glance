"""Ports the controller drives: overlay, capture link, notifications, tray, grammar."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from glance.grammar.issues import Issue
from glance.protocol import ApplyRequest, Rect
from glance.ui.chip_model import ChipView
from glance.ui.panel_model import PanelView
from glance.ui.placement import Anchor
from glance.ui.strip_model import StripView


class OverlayPort(Protocol):
    def show_chip(self, view: ChipView, x: int, y: int) -> None:
        """The chip's state at (x, y). Only the quiet states (`shows_dot`) are drawn, as a
        small dot; the rest are the strip's to show."""
        ...

    def hide_chip(self) -> None: ...

    def show_strip(self, view: StripView, anchor: Anchor) -> None:
        """The collapsed suggestion under the field; it follows the field's window."""
        ...

    def hide_strip(self) -> None: ...

    def show_panel(self, view: PanelView, x: int, y: int, anchor: Anchor | None = None) -> None:
        """The field's drawer (placed by `anchor`, under the strip) or, without an anchor,
        select-anything's panel at (x, y)."""
        ...

    def hide_panel(self) -> None: ...
    def monitors(self) -> list[Rect]: ...
    def copy_to_clipboard(self, text: str) -> None: ...

    def ask_text(self, prompt: str, on_done: Callable[[str, bool], None]) -> None:
        """Ask the user for one line of text in a window that takes the keyboard focus.

        When they submit, the window that was active before is activated again and
        `on_done(text, focus_returned)` is called once it is - or with `focus_returned`
        False when it did not come back in time. Cancelling calls nothing.
        """
        ...


class GhostPort(Protocol):
    """Grey completion text right after the caret: click-through, never takes the focus."""

    def show(self, text: str, x: int, y: int, font_px: int, max_width: int) -> None: ...
    def hide(self) -> None: ...


class CapturePort(Protocol):
    """The link to glance-capture. Results come back through the controller's handlers."""

    connected: bool

    def send_apply(self, req: ApplyRequest) -> None: ...
    def send_revert(self) -> None: ...
    def send_paste(self, text: str, window: int = 0, wm_class: str = "") -> None:
        """Paste `text` into `window` (X id) once, unverified; its result is an APPLY_RESULT
        with apply_id "paste"."""
        ...
    def send_reset(self) -> None: ...


@dataclass(frozen=True)
class WindowInfo:
    """A top-level window: its WM_CLASS class, title, pid, X id and frame, when known."""

    wm_class: str
    title: str
    pid: int
    xid: int
    rect: Rect | None


class SelectionPort(Protocol):
    """The PRIMARY selection and the windows around it."""

    def primary_text(self, max_bytes: int) -> str | None:
        """The selected text, or None when there is none or it is larger than `max_bytes`."""
        ...

    def owner(self) -> WindowInfo | None:
        """The top-level window of whoever owns PRIMARY, None when unknown."""
        ...

    def active(self) -> WindowInfo | None: ...

    def pointer(self) -> tuple[int, int]: ...


class NotifierPort(Protocol):
    def notify(self, summary: str, body: str) -> None: ...


class TrayPort(Protocol):
    def set_state(self, state: str) -> None:
        """`state` is one of active | paused | private | degraded | no_ai."""
        ...


class GrammarPort(Protocol):
    alive: bool

    def check(self, text: str) -> list[Issue]: ...
