"""MacSessionEvents: sleep/wake, screen lock and display changes, from NSWorkspace and the
distributed notification centre, with the same callbacks as `daemon/session.py`.
Needs PyObjC; imported only on a Mac. Callbacks run on the main thread.
"""
from __future__ import annotations

from collections.abc import Callable

import objc
from AppKit import NSApplicationDidChangeScreenParametersNotification, NSWorkspace
from Foundation import NSDistributedNotificationCenter, NSNotificationCenter, NSObject

from glance.daemon.session import SessionEvents
from glance.log import log_event


def _call(fn: Callable[[], None]) -> None:
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        log_event("session_event_error", kind=type(e).__name__)


class _Observer(NSObject):
    def initWithEvents_(self, events):
        self = objc.super(_Observer, self).init()  # noqa: PLW0642 - PyObjC init idiom
        if self is None:
            return None
        self.events = events
        return self

    def willSleep_(self, _n):
        _call(lambda: self.events.prepare_for_sleep(True))

    def didWake_(self, _n):
        _call(lambda: self.events.prepare_for_sleep(False))

    def locked_(self, _n):
        _call(lambda: self.events.active_changed(True))

    def unlocked_(self, _n):
        _call(lambda: self.events.active_changed(False))

    def screens_(self, _n):
        _call(self.events.monitors_changed)


class MacSessionEvents(SessionEvents):
    def start(self) -> None:
        self._observer = _Observer.alloc().initWithEvents_(self)
        ws = NSWorkspace.sharedWorkspace().notificationCenter()
        ws.addObserver_selector_name_object_(self._observer, "willSleep:",
                                             "NSWorkspaceWillSleepNotification", None)
        ws.addObserver_selector_name_object_(self._observer, "didWake:",
                                             "NSWorkspaceDidWakeNotification", None)
        ws.addObserver_selector_name_object_(self._observer, "locked:",
                                             "NSWorkspaceSessionDidResignActiveNotification", None)
        ws.addObserver_selector_name_object_(self._observer, "unlocked:",
                                             "NSWorkspaceSessionDidBecomeActiveNotification", None)
        dist = NSDistributedNotificationCenter.defaultCenter()
        dist.addObserver_selector_name_object_(self._observer, "locked:",
                                               "com.apple.screenIsLocked", None)
        dist.addObserver_selector_name_object_(self._observer, "unlocked:",
                                               "com.apple.screenIsUnlocked", None)
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self._observer, "screens:", NSApplicationDidChangeScreenParametersNotification, None)


def screen_locked() -> bool:
    """Whether the console session is locked right now (for the start-up state)."""
    try:
        import Quartz

        d = Quartz.CGSessionCopyCurrentDictionary()
        return bool(d and d.get("CGSSessionScreenIsLocked"))
    except Exception:  # noqa: BLE001
        return False
