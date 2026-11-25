"""Write-back ports: synthetic keys and the clipboard, as the engine needs them."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class KeysAborted(Exception):
    """Typing stopped part-way because the active window changed."""


@dataclass(frozen=True)
class ClipSnapshot:
    """What the clipboard held before a paste. Opaque to the engine; the adapter that made
    it knows what `payload` is."""

    payload: object


class KeyPort(Protocol):
    """Synthetic keyboard input to the focused window.

    Key names for `chord`: the modifiers `ctrl`, `shift`, `alt`, `super`, then one X keysym
    name (`v`, `z`, `Left`, `Right`, `BackSpace`, ...). The last name is tapped while the
    others are held.
    """

    def focus_target(self) -> tuple[int, int]:
        """(window id, pid) of the window synthetic keys go to right now.

        (0, 0) when that cannot be said for certain: no active window, a window without a
        pid, or the keyboard focus is not in the active window (the shell has it).
        """
        ...

    def any_key_down(self) -> bool:
        """True if any key is physically down at this moment; does not wait."""
        ...

    def can_type(self, text: str) -> bool:
        """True if `type_text` would produce exactly `text` with the keyboard as it is now."""
        ...

    def type_text(self, text: str, delay_ms: int, window: int | None = None) -> None:
        """Type `text` into `window` (default: the current focus target).

        The target is checked before every single key; `KeysAborted` is raised, with the
        rest unsent, as soon as it is not `window` any more.
        """
        ...

    def chord(self, *keys: str) -> None: ...

    def wait_modifiers_released(self, timeout_ms: int = 1000) -> bool:
        """Block until no key at all is physically down; False on timeout.

        While a hotkey's keys are still held, synthetic keys arrive as shortcuts or go to
        the grabbing client instead of the application (findings S4.8).
        """
        ...


class ClipboardPort(Protocol):
    def save(self) -> ClipSnapshot | None:
        """The current clipboard content, or None when it cannot be restored faithfully
        (more than 8 MB, or an owner that does not hand over its data)."""
        ...

    def set_secret_text(self, text: str, target_window: int) -> None:
        """Offer `text` for one paste to the client of `target_window`, and to nobody else.

        Clipboard managers must not get it (findings S4.5); the offer also carries
        `x-kde-passwordManagerHint = secret`.
        """
        ...

    def restore(self, snap: ClipSnapshot) -> None:
        """Put the saved content back, once the pending paste has been read."""
        ...
