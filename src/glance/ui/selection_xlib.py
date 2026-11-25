"""XlibSelection: the SelectionPort over Xlib (needs a live X session).

Reads PRIMARY the way any client does (XConvertSelection to a window of its own) and names
the window that owns it: the owner of a selection is often a child window or a hidden helper
window, so it is resolved to its top-level window, the one the window manager lists.

`primary_text` may wait up to READ_TIMEOUT_S for the owner, so glanced calls it on a worker
thread. It has an X connection of its own: python-xlib's Display must never be used from two
threads at once, and the other methods run on the main loop.
"""
from __future__ import annotations

import select
import threading
import time
from typing import Any

from Xlib import X, Xatom, display, error

from glance.ui.ports import WindowInfo

READ_TIMEOUT_S = 1.0
_MAX_DEPTH = 32  # a window tree is never this deep; a loop must not hang the daemon


def _raw(value: Any) -> bytes:
    """The bytes of a format-8 property: python-xlib hands them over as latin-1 text."""
    return value if isinstance(value, bytes) else str(value).encode("latin-1")


class XlibSelection:
    def __init__(self) -> None:
        self._d = display.Display()
        self._root = self._d.screen().root
        atom = self._d.intern_atom
        self._primary = Xatom.PRIMARY
        self._utf8 = atom("UTF8_STRING")
        self._incr = atom("INCR")
        self._prop = atom("GLANCE_SELECTION")
        self._active = atom("_NET_ACTIVE_WINDOW")
        self._clients = atom("_NET_CLIENT_LIST")
        self._pid = atom("_NET_WM_PID")
        self._name = atom("_NET_WM_NAME")
        # The reader's own connection and window (atoms are the server's: the same ids).
        self._rd = display.Display()
        self._win = self._rd.screen().root.create_window(
            0, 0, 1, 1, 0, X.CopyFromParent, X.InputOnly, event_mask=X.PropertyChangeMask)
        self._rd.flush()
        self._read_lock = threading.Lock()  # one read at a time on that connection

    def close(self) -> None:
        self._rd.close()
        self._d.close()

    # ------------------------------------------------------------------ SelectionPort

    def primary_text(self, max_bytes: int) -> str | None:
        with self._read_lock:
            return self._read_primary(max_bytes)

    def _read_primary(self, max_bytes: int) -> str | None:
        rd = self._rd
        if rd.get_selection_owner(self._primary) == X.NONE:
            return None
        self._drain()
        self._win.convert_selection(self._primary, self._utf8, self._prop, X.CurrentTime)
        rd.flush()
        if not self._wait_notify():
            return None
        try:
            prop = self._win.get_full_property(self._prop, X.AnyPropertyType)
            self._win.delete_property(self._prop)
            rd.flush()
        except error.XError:
            return None
        if prop is None or prop.property_type == self._incr:
            return None  # a transfer in chunks is far beyond any usable selection
        data = _raw(prop.value)
        if prop.property_type != self._utf8 or len(data) > max_bytes:
            return None
        return data.decode("utf-8", errors="replace")

    def owner(self) -> WindowInfo | None:
        try:
            owner = self._d.get_selection_owner(self._primary)
            if owner == X.NONE:
                return None
            top = self._toplevel(owner)
            if top is not None:
                return self._info(top)
            # A helper window outside the client's tree (GTK owns the selection with one).
            # It counts for a top-level window only when its process has exactly one: with
            # two, a stale selection of one window could pass as the other's (a private
            # window next to a normal one).
            pid = self._window_pid(owner)
            same = [w for w in self._client_windows() if pid and self._window_pid(w) == pid]
            return self._info(same[0]) if len(same) == 1 else None
        except error.XError:
            pass
        return None

    def active(self) -> WindowInfo | None:
        try:
            prop = self._root.get_full_property(self._active, X.AnyPropertyType)
            xid = int(prop.value[0]) if prop is not None and len(prop.value) else 0
            if not xid:
                return None
            return self._info(self._d.create_resource_object("window", xid))
        except error.XError:
            return None

    def pointer(self) -> tuple[int, int]:
        q = self._root.query_pointer()
        return int(q.root_x), int(q.root_y)

    # ------------------------------------------------------------------ helpers

    def _drain(self) -> None:
        while self._rd.pending_events():
            self._rd.next_event()

    def _wait_notify(self) -> bool:
        deadline = time.monotonic() + READ_TIMEOUT_S
        while True:
            while self._rd.pending_events():
                ev = self._rd.next_event()
                if ev.type == X.SelectionNotify and ev.requestor.id == self._win.id:
                    return ev.property != X.NONE
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            select.select([self._rd], [], [], left)

    def _client_windows(self) -> list[Any]:
        prop = self._root.get_full_property(self._clients, X.AnyPropertyType)
        ids = [int(i) for i in prop.value] if prop is not None else []
        return [self._d.create_resource_object("window", i) for i in ids]

    def _toplevel(self, window: Any) -> Any:
        """The ancestor of `window` (or itself) that has a WM_CLASS: the client's top-level."""
        w = window
        for _ in range(_MAX_DEPTH):
            if w.get_wm_class() is not None:
                return w
            tree = w.query_tree()
            if tree.parent is None or tree.parent.id == self._root.id:
                return None
            w = tree.parent
        return None

    def _window_pid(self, window: Any) -> int:
        prop = window.get_full_property(self._pid, X.AnyPropertyType)
        return int(prop.value[0]) if prop is not None and len(prop.value) else 0

    def _info(self, window: Any) -> WindowInfo | None:
        cls = window.get_wm_class()
        if cls is None:
            return None
        name = window.get_full_property(self._name, X.AnyPropertyType)
        title = _raw(name.value).decode("utf-8", errors="replace") if name is not None \
            else (window.get_wm_name() or "")
        return WindowInfo(wm_class=cls[1] or cls[0], title=title,
                          pid=self._window_pid(window), xid=int(window.id), rect=None)
