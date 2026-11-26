"""MacNotifier: the NotifierPort over `osascript` (`display notification`).

The AppleScript goes in on stdin, never on argv; the strings are Glance's own messages
(never typed text). Shown on a thread of its own, in order, like the Linux notifier.
"""
from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor

from glance.log import log_event


def applescript_string(s: str) -> str:
    """`s` as an AppleScript string literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notification_script(summary: str, body: str) -> str:
    return (f"display notification {applescript_string(body)} "
            f"with title {applescript_string(summary)}")


class MacNotifier:
    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="glance-notify")

    def notify(self, summary: str, body: str) -> None:
        log_event("notify")
        self._pool.submit(self._show, summary, body)

    @staticmethod
    def _show(summary: str, body: str) -> None:
        try:
            subprocess.run(["osascript", "-"], input=notification_script(summary, body),
                           text=True, capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError) as e:
            log_event("notify_failed", kind=type(e).__name__)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
