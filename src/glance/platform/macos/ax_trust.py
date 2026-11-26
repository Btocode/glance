"""glanced's Accessibility permission: ask macOS once, then notice the switch going on.

macOS grants Accessibility per executable, and glanced runs as the Python binary uv
installed. On a start without the permission, glanced calls
AXIsProcessTrustedWithOptions({prompt: True}) - once per binary path, remembered in a state
file - so macOS shows its own "would like to control this computer" dialog and adds the
binary to the Accessibility list switched off: the user only flips the switch. Until then
glanced asks AXIsProcessTrusted() every POLL_MS and, when it turns true, calls `on_trusted`
(glanced restarts its capture helper), so no `launchctl kickstart` is needed.

Pure: the AX calls, the timer and the state file are passed in (tests use fakes).
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from glance.log import log_event

POLL_MS = 2000
AX_PANE = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
STATE_FILE = "ax-prompted"  # in glanced's state dir: one binary path per line


def open_pane_argv() -> list[str]:
    """The command that opens System Settings at Privacy & Security → Accessibility."""
    return ["open", AX_PANE]


def prompted_paths(state_file: Path) -> set[str]:
    try:
        return {ln for ln in state_file.read_text().splitlines() if ln}
    except OSError:
        return set()


def remember_prompted(state_file: Path, exe: str) -> None:
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        with state_file.open("a") as f:
            f.write(exe + "\n")
    except OSError as e:
        log_event("ax_prompt_state_unwritable", kind=type(e).__name__)


class AxTrustWatch:
    def __init__(self, is_trusted: Callable[[], bool], prompt: Callable[[], Any],
                 every: Callable[[int, Callable[[], None]], Any], exe: str,
                 state_file: Path, on_trusted: Callable[[], None]) -> None:
        self._is_trusted, self._prompt, self._every = is_trusted, prompt, every
        self._exe, self._state_file, self._on_trusted = exe, state_file, on_trusted
        self._timer: Any = None
        self.trusted = False
        self.prompted = False  # macOS's dialog was asked for, now or on an earlier start

    @property
    def waiting(self) -> bool:
        """Polling for the switch to go on (glanced picks it up without a restart)."""
        return self._timer is not None

    def start(self) -> bool:
        """True when already trusted. Otherwise ask macOS once and start polling."""
        if self._check():
            return True
        self.prompted = self._exe in prompted_paths(self._state_file)
        if not self.prompted:
            self.prompt()
        log_event("ax_not_trusted", waiting=True)
        self._timer = self._every(POLL_MS, self._poll)
        return False

    def prompt(self) -> None:
        """Show macOS's dialog (it also adds the binary to the list) and remember it."""
        try:
            self._prompt()
        except Exception as e:  # noqa: BLE001 - no dialog is not a reason to stop
            log_event("ax_prompt_failed", kind=type(e).__name__)
            return
        if not self.prompted:
            remember_prompted(self._state_file, self._exe)
        self.prompted = True
        log_event("ax_prompted")

    def _check(self) -> bool:
        try:
            self.trusted = bool(self._is_trusted())
        except Exception:  # noqa: BLE001
            self.trusted = False
        return self.trusted

    def _poll(self) -> None:
        if self._timer is None or not self._check():
            return
        timer, self._timer = self._timer, None
        timer.cancel()
        log_event("ax_trusted_now")
        self._on_trusted()
