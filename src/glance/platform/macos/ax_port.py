"""AX adapter for the capture ports: the focused text field over the macOS Accessibility API.

Facts this module encodes:

- The application that has the keyboard: the system-wide element's `AXFocusedApplication`,
  and when macOS refuses that query (it answers kAXErrorCannotComplete at once for a process
  outside the console's security session, e.g. one started over SSH, and for some
  front apps) the frontmost `NSRunningApplication`, whose own AX element is then asked for
  its focused element (`focused_app`).
- AX ranges count UTF-16 units; `FieldRead` offsets are Python indices (`ax_text`).
- AX rects are top-left global coordinates already: they go on the wire unchanged.
- A secure text field (`AXSecureTextField`) is reported with role `password text` and its
  value is never requested; the gate refuses it before `read` is ever called.
- Chromium and Electron build their tree only when asked (`ax_roles.wants_manual_accessibility`).
- Every call has a messaging timeout; a timeout or a vanished element is `A11yTimeout`.

Needs PyObjC (ApplicationServices); imported only on a Mac, in glance-capture.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import ApplicationServices as AX
import CoreFoundation as CF
import objc
from AppKit import NSRunningApplication

from glance.capture.ports import A11yField, A11yTimeout, FieldRead
from glance.log import log_event
from glance.platform.macos import ax_roles
from glance.platform.macos.ax_text import range_to_py, range_to_utf16, utf16_len
from glance.platform.macos.geometry import ax_rect, caret_from_bounds
from glance.privacy.gate import FieldMeta
from glance.protocol import Rect

TIMEOUT_S = 0.25
POLL_MS = 250
_MAX_ANCESTORS = 48
_GONE = {AX.kAXErrorInvalidUIElement, AX.kAXErrorCannotComplete}
_APP_NOTES = ("AXFocusedUIElementChanged", "AXFocusedWindowChanged", "AXValueChanged",
              "AXSelectedTextChanged", "AXWindowMoved", "AXWindowResized")
_FIELD_NOTES = ("AXValueChanged", "AXSelectedTextChanged")


def _get(el: Any, attr: str) -> tuple[int, Any]:
    err, value = AX.AXUIElementCopyAttributeValue(el, attr, None)
    return err, value


def _opt(el: Any, attr: str) -> Any:
    """The attribute's value, or None for any error (missing, unsupported, timeout)."""
    if el is None:
        return None
    err, value = _get(el, attr)
    return value if err == AX.kAXErrorSuccess else None


BUSY_RETRIES = 6
BUSY_WAIT_S = 0.15


def _must(el: Any, attr: str) -> Any:
    """The attribute's value; `A11yTimeout` when the app does not answer or the element is
    gone; None when it simply has no such attribute.

    An app answers AX only from its main run loop's default mode. Right after a menu key
    equivalent (the ⌘V of a paste, a ⌘Z) AppKit flashes the menu title in a tracking loop and
    AX gets kAXErrorCannotComplete for a moment though the app is fine: that is asked again a
    few times (about a second in all) before the field counts as not responding."""
    for _ in range(BUSY_RETRIES):
        err, value = _get(el, attr)
        if err != AX.kAXErrorCannotComplete:
            break
        time.sleep(BUSY_WAIT_S)
    if err in _GONE:
        raise A11yTimeout(attr)
    return value if err == AX.kAXErrorSuccess else None


def _settable(el: Any, attr: str) -> bool:
    err, ok = AX.AXUIElementIsAttributeSettable(el, attr, None)
    return err == AX.kAXErrorSuccess and bool(ok)


def _point(v: Any) -> tuple[float, float] | None:
    if v is None:
        return None
    ok, p = AX.AXValueGetValue(v, AX.kAXValueCGPointType, None)
    return (p.x, p.y) if ok else None


def _size(v: Any) -> tuple[float, float] | None:
    if v is None:
        return None
    ok, s = AX.AXValueGetValue(v, AX.kAXValueCGSizeType, None)
    return (s.width, s.height) if ok else None


def _range(v: Any) -> tuple[int, int] | None:
    if v is None:
        return None
    ok, r = AX.AXValueGetValue(v, AX.kAXValueCFRangeType, None)
    if not ok:
        return None
    if isinstance(r, tuple):  # PyObjC hands a CFRange back as (location, length)
        return int(r[0]), int(r[1])
    return int(r.location), int(r.length)


def _frame(el: Any) -> Rect | None:
    return ax_rect(_point(_opt(el, "AXPosition")), _size(_opt(el, "AXSize")))


def _bounds(el: Any, loc: int, length: int) -> tuple[float, float, float, float] | None:
    rng = AX.AXValueCreate(AX.kAXValueCFRangeType, CF.CFRangeMake(loc, length))
    err, v = AX.AXUIElementCopyParameterizedAttributeValue(el, "AXBoundsForRange", rng, None)
    if err != AX.kAXErrorSuccess or v is None:
        return None
    ok, r = AX.AXValueGetValue(v, AX.kAXValueCGRectType, None)
    return (r.origin.x, r.origin.y, r.size.width, r.size.height) if ok else None


def _url_string(v: Any) -> str | None:
    if v is None:
        return None
    s = v.absoluteString() if hasattr(v, "absoluteString") else v
    return str(s) if s else None


def focused_app(system: Any) -> tuple[int, Any] | None:
    """(pid, AX application element) of the app that has the keyboard, or None.

    The system-wide `AXFocusedApplication` first; when that is refused, the frontmost app as
    NSWorkspace knows it (kept current by the run loop this process runs)."""
    err, app = AX.AXUIElementCopyAttributeValue(system, "AXFocusedApplication", None)
    if err == AX.kAXErrorSuccess and app is not None:
        err, pid = AX.AXUIElementGetPid(app, None)
        if err == AX.kAXErrorSuccess and pid:
            AX.AXUIElementSetMessagingTimeout(app, TIMEOUT_S)
            return int(pid), app
    from AppKit import NSWorkspace

    front = NSWorkspace.sharedWorkspace().frontmostApplication()
    if front is None:
        return None
    pid = int(front.processIdentifier())
    if pid <= 0:
        return None
    app = AX.AXUIElementCreateApplication(pid)
    AX.AXUIElementSetMessagingTimeout(app, TIMEOUT_S)
    return pid, app


def element_key(pid: int, el: Any) -> str:
    """A stable id for one element: equal AX elements hash equal (CFHash follows CFEqual)."""
    return f"ax:{pid}:{CF.CFHash(el):x}"


def window_token(win: Any) -> int:
    """A nonzero int naming one window, the same for every copy of its element."""
    return (int(CF.CFHash(win)) & 0x7FFFFFFFFFFF) or 1


class AxField:
    """One focused text element. Every method may raise `A11yTimeout`."""

    def __init__(self, port: AxPort, el: Any, pid: int, bundle: str, app: str) -> None:
        self._port = port
        self.el = el
        self.pid = pid
        self.bundle = bundle
        self.app_name = app
        self.key = element_key(pid, el)
        role = _opt(el, "AXRole")
        subrole = _opt(el, "AXSubrole")
        self.role = str(role) if role else ""
        self.subrole = str(subrole) if subrole else ""
        self.secure = ax_roles.is_secure(self.role, self.subrole)
        AX.AXUIElementSetMessagingTimeout(el, TIMEOUT_S)

    # ------------------------------------------------------------------ A11yField

    def window(self) -> Any:
        return _opt(self.el, "AXWindow")

    def meta(self) -> FieldMeta:
        win = self.window()
        title = _opt(win, "AXTitle") if win is not None else None
        url = self._url()
        editable = (not self.secure) and (
            _settable(self.el, "AXValue") or _opt(self.el, "AXEditableAncestor") is not None)
        if self.secure:
            n = 0  # nothing about a secure field's content is asked for, not even its length
        else:
            n = _opt(self.el, "AXNumberOfCharacters")
            n = int(n) if isinstance(n, int) else 0
        multi = self.role == "AXTextArea"
        return FieldMeta(
            app=self.app_name, wm_class=self.bundle, title=str(title or ""), url=url,
            role=ax_roles.role_name(self.role, self.subrole),
            states=ax_roles.states(editable=editable, secure=self.secure, multi_line=multi),
            pid=self.pid, text_len=n,
        )

    def window_rect(self) -> Rect | None:
        win = self.window()
        return _frame(win) if win is not None else None

    def field_extents(self) -> Rect | None:
        return _frame(self.el)

    def read(self) -> FieldRead:
        if self.secure:
            raise A11yTimeout("secure field")  # never reached through the gate; fail closed
        value = _must(self.el, "AXValue")
        if value is None:
            value = ""
        if not isinstance(value, str):
            value = str(value.string()) if hasattr(value, "string") else ""
        text = str(value)
        rng = _range(_must(self.el, "AXSelectedTextRange"))
        if rng is None:
            caret, selection = len(text), None
        else:
            start, end = range_to_py(text, rng[0], rng[1])
            caret = end
            selection = (start, end) if end > start else None
        screens = self._port.screens()
        caret_rect = prev_rect = None
        if rng is not None:
            u = rng[0] + rng[1]
            if u > 0 and text[caret - 1:caret] != "\n":
                prev_rect = caret_from_bounds(_bounds(self.el, u - 1, 1), screens)
            if prev_rect is not None:
                # NSTextView answers a zero-length range with the line *above* at the end of
                # a line; the right edge of the previous character is where the caret is.
                caret_rect = Rect(prev_rect.x + prev_rect.w, prev_rect.y, 1, prev_rect.h)
            elif u < utf16_len(text):
                b = _bounds(self.el, u, 1)
                caret_rect = caret_from_bounds(None if b is None else (b[0], b[1], 1, b[3]),
                                               screens)
            if caret_rect is None:
                caret_rect = caret_from_bounds(_bounds(self.el, u, 0), screens)
        return FieldRead(text=text, caret=caret, selection=selection, caret_rect=caret_rect,
                         prev_char_rect=prev_rect, field_rect=_frame(self.el),
                         window_rect=self.window_rect())

    def set_selection(self, start: int, end: int) -> bool:
        text = self._text()
        loc, length = range_to_utf16(text, start, end)
        return self._set_range(loc, length)

    def set_caret(self, offset: int) -> bool:
        text = self._text()
        loc, _ = range_to_utf16(text, offset, offset)
        return self._set_range(loc, 0)

    # ------------------------------------------------------------------ internals

    def _text(self) -> str:
        if self.secure:
            raise A11yTimeout("secure field")
        v = _must(self.el, "AXValue")
        return v if isinstance(v, str) else ""

    def _set_range(self, loc: int, length: int) -> bool:
        rng = AX.AXValueCreate(AX.kAXValueCFRangeType, CF.CFRangeMake(loc, length))
        err = AX.AXUIElementSetAttributeValue(self.el, "AXSelectedTextRange", rng)
        if err in _GONE:
            raise A11yTimeout("set range")
        return err == AX.kAXErrorSuccess

    def _url(self) -> str | None:
        """The page URL of the web area holding the element, when it is web content."""
        if not (self.bundle in ax_roles.MAC_BROWSERS
                or _opt(self.el, "AXEditableAncestor") is not None):
            return None
        el = self.el
        for _ in range(_MAX_ANCESTORS):
            if el is None:
                return None
            if _opt(el, "AXRole") == ax_roles.WEB_AREA:
                return _url_string(_opt(el, "AXURL")) or _url_string(_opt(el, "AXDocument"))
            el = _opt(el, "AXParent")
        return None


_PORTS: list[AxPort] = []  # the live port(s) the C observer callback hands notifications to


@objc.callbackFor(AX.AXObserverCreate)
def _observer_callback(observer: Any, element: Any, note: str, refcon: Any) -> None:
    for port in _PORTS:  # on the observer thread: hand it to the main loop
        port._post(lambda p=port, n=str(note): p._notified(None, None, n, None))


class _ObserverThread:
    """A thread whose run loop does nothing but receive AX notifications.

    An application delivers its notifications to an observer synchronously, with a timeout.
    If they went to the main run loop, a notification sent while the main thread sits in an
    AX call to that same application (the write-back engine re-reading the field just after
    a paste or keys) would leave both waiting: the app does not answer (kAXErrorCannotComplete)
    until its send times out. Seen on macOS 15.6. Here they are always taken at once."""

    def __init__(self) -> None:
        import threading

        self.loop: Any = None
        ready = threading.Event()

        def run() -> None:
            self.loop = CF.CFRunLoopGetCurrent()
            # a source that never fires keeps the loop alive before the first observer
            timer = CF.CFRunLoopTimerCreate(None, CF.CFAbsoluteTimeGetCurrent() + 1e9, 1e9, 0,
                                            0, lambda *_a: None, None)
            CF.CFRunLoopAddTimer(self.loop, timer, CF.kCFRunLoopDefaultMode)
            ready.set()
            CF.CFRunLoopRun()

        threading.Thread(target=run, name="glance-ax-observer", daemon=True).start()
        ready.wait(5)

    def add(self, obs: Any) -> None:
        CF.CFRunLoopAddSource(self.loop, AX.AXObserverGetRunLoopSource(obs),
                              CF.kCFRunLoopDefaultMode)
        CF.CFRunLoopWakeUp(self.loop)

    def remove(self, obs: Any) -> None:
        CF.CFRunLoopRemoveSource(self.loop, AX.AXObserverGetRunLoopSource(obs),
                                 CF.kCFRunLoopDefaultMode)


class AxPort:
    """A11yPort over AX observers plus a short focus poll (some apps never post a focus
    change). Runs on the capture process's main run loop."""

    def __init__(self, call_every: Callable[[int, Callable[[], None]], Any],
                 screens: Callable[[], list[Rect]] = list,
                 post: Callable[[Callable[[], None]], None] | None = None) -> None:
        self._every = call_every
        self._post = post if post is not None else (lambda fn: fn())
        self._thread = _ObserverThread()
        self.screens = screens
        self._system = AX.AXUIElementCreateSystemWide()
        AX.AXUIElementSetMessagingTimeout(self._system, TIMEOUT_S)
        self._on_focus: Callable[[A11yField | None], None] | None = None
        self._on_change: Callable[[A11yField], None] | None = None
        self._field: AxField | None = None
        self._observers: dict[int, Any] = {}  # pid -> AXObserver
        self._observed_field: Any = None
        self._woken: set[int] = set()
        self._poll: Any = None
        _PORTS.append(self)

    # ------------------------------------------------------------------ A11yPort

    def start(self, on_focus: Callable[[A11yField | None], None],
              on_change: Callable[[A11yField], None]) -> None:
        self._on_focus, self._on_change = on_focus, on_change
        self._poll = self._every(POLL_MS, self._check_focus)

    def focused(self) -> AxField | None:
        try:
            self._field = self._read_focus()
        except A11yTimeout:
            self._field = None
        self._observe_field(self._field)
        return self._field

    def stop(self) -> None:
        if self._poll is not None:
            self._poll.cancel()
        for obs in self._observers.values():
            self._thread.remove(obs)
        self._observers.clear()

    def reregister(self) -> None:
        self.stop()
        self._woken.clear()
        if self._on_focus is not None and self._on_change is not None:
            self.start(self._on_focus, self._on_change)

    def focused_app(self) -> tuple[int, Any] | None:
        return focused_app(self._system)

    # ------------------------------------------------------------------ internals

    def _read_focus(self) -> AxField | None:
        got = self.focused_app()
        if got is None:
            return None
        pid, app = got
        AX.AXUIElementSetMessagingTimeout(app, TIMEOUT_S)
        running = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        bundle = str(running.bundleIdentifier() or "") if running is not None else ""
        name = str(running.localizedName() or "") if running is not None else ""
        self._wake(pid, app, bundle)
        self._observe_app(pid, app)
        el = _must(app, "AXFocusedUIElement")
        if el is None:
            return None
        role, subrole = _opt(el, "AXRole"), _opt(el, "AXSubrole")
        editable_ancestor = _opt(el, "AXEditableAncestor") is not None
        if not ax_roles.is_text_role(role, subrole, editable_ancestor):
            return None
        if self._field is not None and CF.CFEqual(self._field.el, el):
            return self._field
        return AxField(self, el, pid, bundle, name)

    def _check_focus(self) -> None:
        before = self._field
        try:
            now = self._read_focus()
        except A11yTimeout:
            now = None
        if (before is None) != (now is None) or (
                before is not None and now is not None and before.key != now.key):
            self._field = now
            self._observe_field(now)
            if self._on_focus is not None:
                self._on_focus(now)

    def _wake(self, pid: int, app: Any, bundle: str) -> None:
        """Ask Chromium / Electron to build their accessibility tree, once per process."""
        if pid in self._woken:
            return
        self._woken.add(pid)
        attr = ax_roles.wants_manual_accessibility(bundle)
        if attr is None:
            return
        err = AX.AXUIElementSetAttributeValue(app, attr, True)
        log_event("ax_manual_accessibility", ok=err == AX.kAXErrorSuccess)

    def _observe_app(self, pid: int, app: Any) -> None:
        if pid in self._observers:
            return
        err, obs = AX.AXObserverCreate(pid, _observer_callback, None)
        if err != AX.kAXErrorSuccess or obs is None:
            log_event("ax_observer_failed", err=int(err))
            return
        for note in _APP_NOTES:
            AX.AXObserverAddNotification(obs, app, note, None)
        self._thread.add(obs)
        self._observers[pid] = obs
        if len(self._observers) > 32:  # apps come and go: keep the newest
            old = next(iter(self._observers))
            if old != pid:
                gone = self._observers.pop(old)
                self._thread.remove(gone)

    def _observe_field(self, field: AxField | None) -> None:
        if field is None or self._observed_field is field.el:
            return
        obs = self._observers.get(field.pid)
        if obs is None:
            return
        for note in _FIELD_NOTES:
            AX.AXObserverAddNotification(obs, field.el, note, None)
        self._observed_field = field.el

    def _notified(self, _observer: Any, _element: Any, note: str, _refcon: Any) -> None:
        try:
            if note in ("AXFocusedUIElementChanged", "AXFocusedWindowChanged"):
                self._check_focus()
            elif self._field is not None and self._on_change is not None:
                self._on_change(self._field)
        except Exception as e:  # noqa: BLE001 - never unwind into the AX callback
            log_event("ax_callback_error", kind=type(e).__name__)
