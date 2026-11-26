"""MacClock: the `Clock` port over the main NSRunLoop, and a small run-loop driver.

`call_later` is an `NSTimer`, `post` (from any thread) hands the function to the main thread
with `performSelectorOnMainThread:`; both run in the common modes, so they still fire while a
menu is open. Needs PyObjC (Foundation); imported only on a Mac.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

import objc
from Foundation import NSDate, NSObject, NSRunLoop, NSRunLoopCommonModes, NSTimer

from glance.log import log_event


def _run_safely(fn: Callable[[], None]) -> None:
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - an exception must not unwind the run loop
        log_event("main_loop_error", kind=type(e).__name__)


class _Poster(NSObject):
    """Receives `drain:` on the main thread and runs what `post` queued, in order."""

    def initWithQueue_(self, queue):
        self = objc.super(_Poster, self).init()  # noqa: PLW0642 - PyObjC init idiom
        if self is None:
            return None
        self.queue = queue
        return self

    def drain_(self, _arg):
        while True:
            try:
                fn = self.queue.popleft()
            except IndexError:
                return
            _run_safely(fn)


class _Handle:
    def __init__(self) -> None:
        self.timer = None

    def cancel(self) -> None:
        if self.timer is not None:
            self.timer.invalidate()
            self.timer = None


class MacClock:
    """Clock backed by the main run loop. Construct it on the main thread."""

    def __init__(self) -> None:
        self._queue: deque[Callable[[], None]] = deque()
        self._poster = _Poster.alloc().initWithQueue_(self._queue)
        self._loop = NSRunLoop.mainRunLoop()

    def now(self) -> float:
        return time.monotonic()

    def call_later(self, ms: int, fn: Callable[[], None]) -> _Handle:
        handle = _Handle()

        def fire(_timer: object) -> None:
            handle.timer = None
            _run_safely(fn)

        timer = NSTimer.timerWithTimeInterval_repeats_block_(max(0, ms) / 1000.0, False, fire)
        handle.timer = timer
        if threading.current_thread() is threading.main_thread():
            self._loop.addTimer_forMode_(timer, NSRunLoopCommonModes)
        else:  # NSRunLoop is not thread-safe: add it from the main thread
            self.post(lambda: self._loop.addTimer_forMode_(timer, NSRunLoopCommonModes)
                      if handle.timer is timer else None)
        return handle

    def post(self, fn: Callable[[], None]) -> None:
        self._queue.append(fn)
        self._poster.performSelectorOnMainThread_withObject_waitUntilDone_modes_(
            "drain:", None, False, [NSRunLoopCommonModes])

    def every(self, ms: int, fn: Callable[[], None]) -> _Handle:
        """A repeating timer (the child poll, the capture focus poll)."""
        handle = _Handle()
        timer = NSTimer.timerWithTimeInterval_repeats_block_(
            max(1, ms) / 1000.0, True, lambda _t: _run_safely(fn))
        handle.timer = timer
        self._loop.addTimer_forMode_(timer, NSRunLoopCommonModes)
        return handle


class ConsoleLoop:
    """Run the main run loop without an NSApplication (glance-capture) until `stop()`.

    It returns to Python at least every `tick_s`, so signal handlers run promptly.
    """

    def __init__(self, tick_s: float = 0.25) -> None:
        self._tick = tick_s
        self._stopped = False

    def run(self) -> None:
        loop = NSRunLoop.mainRunLoop()
        while not self._stopped:
            loop.runMode_beforeDate_("kCFRunLoopDefaultMode",
                                     NSDate.dateWithTimeIntervalSinceNow_(self._tick))

    def stop(self) -> None:
        self._stopped = True

    @staticmethod
    def pumping_sleep(seconds: float) -> None:
        """Wait `seconds` while the run loop keeps delivering AX notifications. An app whose
        notifications to this process pile up undelivered stops answering AX requests
        (kAXErrorCannotComplete) until they drain, so the write-back engine must not block
        the loop while it waits for an application to catch up."""
        from CoreFoundation import CFRunLoopRunInMode, kCFRunLoopDefaultMode

        end = time.monotonic() + max(0.0, seconds)
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return
            CFRunLoopRunInMode(kCFRunLoopDefaultMode, left, False)

    @staticmethod
    def dispatch_pending(limit: int = 64) -> None:
        """Run what the loop has ready now (focus changes that arrived while an apply ran),
        without blocking; the write-back engine calls this before it trusts the focus."""
        from CoreFoundation import (
            CFRunLoopRunInMode,
            kCFRunLoopDefaultMode,
            kCFRunLoopRunHandledSource,
        )

        for _ in range(limit):
            if CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0, True) != kCFRunLoopRunHandledSource:
                return
