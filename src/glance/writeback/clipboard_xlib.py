"""XlibClipboard: the ClipboardPort as an X selection owner (needs a live X session).

Why not a GTK clipboard (findings S4.5, S4.6):

- The clipboard-history extension reads the text of every new owner and ignores
  `x-kde-passwordManagerHint`. The replacement text is therefore served only to requestors
  that belong to the X client of the window the engine pinned for the write (same
  resource-id base); every other requestor gets a refusal.
- No CLIPBOARD_MANAGER runs in the session, so restored content lives only as long as its
  owner. After a paste this object stays the owner and serves the saved content, every
  target byte for byte, until another application copies something.

All X traffic happens on one worker thread with its own connection, because requests have to
be answered while the engine is blocked between Ctrl+V and the restore. Clipboard content is
held in memory only, and dropped as soon as another client takes the selection.
"""
from __future__ import annotations

import os
import queue
import select
import struct
import threading
import time
from collections.abc import Callable
from typing import Any

from Xlib import X, Xatom, display
from Xlib.protocol import event as xevent
from Xlib.protocol import request as xrequest

from glance.log import log_event
from glance.writeback.ports import ClipSnapshot

MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
CHUNK_BYTES = 200_000      # below the 262140-byte request limit; a multiple of 4
CONVERT_TIMEOUT_S = 0.5    # one answer from the selection owner
SAVE_BUDGET_S = 2.0        # the whole snapshot
COMMAND_TIMEOUT_S = 3.0
PASTE_GRACE_S = 0.1        # keep serving this long after the last read (Chrome reads twice)
PASTE_TIMEOUT_S = 1.0
INCR_EXPIRY_S = 10.0
TEXT_TARGETS = ("UTF8_STRING", "text/plain;charset=utf-8")
HINT_TARGET = "x-kde-passwordManagerHint"
# Targets that are answers about the selection, or that make the owner do something
# (DELETE erases the selected data), rather than a representation of the content.
SKIPPED_TARGETS = frozenset({
    "TARGETS", "MULTIPLE", "TIMESTAMP", "SAVE_TARGETS", "DELETE", "INSERT_SELECTION",
    "INSERT_PROPERTY",
})
_MAX_PROPERTY_LEN = 0x1FFFFFFF  # in 4-byte units: the whole property in one reply

# target atom -> (type atom, format, data); atoms are valid on every connection to the server
Content = dict[int, tuple[int, int, bytes]]


def same_client(window_a: int, window_b: int, id_mask: int) -> bool:
    """True if both resource ids were allocated by the same X client."""
    return bool(window_a and window_b) and (window_a & ~id_mask) == (window_b & ~id_mask)


def chunks(data: bytes, size: int = CHUNK_BYTES) -> list[bytes]:
    """`data` cut into INCR pieces, ending with the empty piece that closes the transfer."""
    return [data[i:i + size] for i in range(0, len(data), size)] + [b""]


def _as_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):  # python-xlib decodes format-8 data that is valid UTF-8
        return value.encode("utf-8")
    return value.tobytes()


def _swallow(_error: Any, _request: Any) -> int:
    """Error handler for requests on other clients' windows, which may be gone already."""
    return 1


class _NotRestorable(Exception):
    """The selection owner did not answer in time, or holds more than can be kept."""


class _Owner(threading.Thread):
    """The worker: one X connection, one unmapped window, the selection conversations."""

    def __init__(self, selection: str, display_name: str | None = None) -> None:
        super().__init__(name="glance-clipboard", daemon=True)
        self._d = display.Display(display_name)
        self._root = self._d.screen().root
        self._win = self._root.create_window(
            0, 0, 1, 1, 0, X.CopyFromParent, event_mask=X.PropertyChangeMask)
        atom = self._d.intern_atom
        self._sel = atom(selection)
        self._targets = atom("TARGETS")
        self._incr_type = atom("INCR")
        self._prop = atom("GLANCE_SELECTION")
        self._text = [atom(name) for name in TEXT_TARGETS]
        self._hint = atom(HINT_TARGET)
        self._skipped = {atom(name) for name in SKIPPED_TARGETS}
        self._id_mask = self._d.display.info.resource_id_mask
        self._commands: queue.Queue = queue.Queue()
        self._wake_r, self._wake_w = os.pipe()
        self._stopping = False
        # What is being served: nothing (None), the replacement, or a restored snapshot.
        self._mode: str | None = None
        self._secret = b""
        self._client = 0
        self._content: Content = {}
        self._incr: dict[tuple[int, int], tuple[Any, int, int, list[bytes], float]] = {}
        # Read by the caller's thread while it waits for the paste to be read.
        self.lock = threading.Lock()
        self.secret_up = False  # the replacement is on offer and has not been withdrawn
        self.served_at: float | None = None
        self.lost = False       # another client took the selection while the secret was up
        self.refused = 0

    # ------------------------------------------------------------------ caller's side

    def call(self, fn: Callable[[], Any], timeout_s: float = COMMAND_TIMEOUT_S) -> Any:
        """Run `fn` on the worker and return its result; TimeoutError if it does not."""
        done = threading.Event()
        box: list[Any] = [None, None]
        self._commands.put((fn, box, done))
        os.write(self._wake_w, b"x")
        if not done.wait(timeout_s):
            raise TimeoutError("clipboard worker")
        if box[1] is not None:
            raise box[1]
        return box[0]

    def stop(self) -> None:
        self._stopping = True
        os.write(self._wake_w, b"x")

    # ------------------------------------------------------------------ worker loop

    def run(self) -> None:
        try:
            while not self._stopping:
                self._run_commands()
                self._drain()
                self._expire_incr()
                ready, _, _ = select.select([self._d.fileno(), self._wake_r], [], [], 0.25)
                if self._wake_r in ready:
                    os.read(self._wake_r, 4096)
        except Exception as e:  # noqa: BLE001 - the thread must not die silently
            log_event("clipboard_worker_died", kind=type(e).__name__)
        finally:
            self._d.close()

    def _run_commands(self) -> None:
        while True:
            try:
                fn, box, done = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                box[0] = fn()
            except Exception as e:  # noqa: BLE001 - handed to the caller
                box[1] = e
            done.set()

    def _drain(self) -> None:
        while self._d.pending_events():
            self._dispatch(self._d.next_event())

    def _dispatch(self, ev: Any) -> None:
        if ev.type == X.SelectionRequest:
            if ev.selection == self._sel and ev.owner.id == self._win.id:
                self._answer(ev)
        elif ev.type == X.SelectionClear:
            if ev.atom == self._sel and ev.window.id == self._win.id:
                with self.lock:
                    self.lost = self.lost or self._mode == "secret"
                self._forget()
        elif ev.type == X.PropertyNotify and ev.state == X.PropertyDelete:
            self._continue_incr(ev.window.id, ev.atom)

    def _wait_for(self, wanted: Callable[[Any], bool], deadline: float) -> Any:
        """The next event `wanted` accepts; other events are handled as usual meanwhile."""
        while True:
            while self._d.pending_events():
                ev = self._d.next_event()
                if wanted(ev):
                    return ev
                self._dispatch(ev)
            left = deadline - time.monotonic()
            if left <= 0:
                raise _NotRestorable
            select.select([self._d.fileno()], [], [], min(left, 0.05))

    # ------------------------------------------------------------------ serving

    def _forget(self) -> None:
        with self.lock:
            self.secret_up = False
        self._mode = None
        self._secret = b""
        self._content = {}
        self._incr.clear()

    def _answer(self, ev: Any) -> None:
        prop = ev.property if ev.property != X.NONE else ev.target  # pre-ICCCM requestors
        requestor, target = ev.requestor, ev.target
        ok = False
        if self._mode == "secret":
            if not same_client(requestor.id, self._client, self._id_mask):
                self.refused += 1
            elif target == self._targets:
                atoms = [self._targets, *self._text, self._hint]
                requestor.change_property(prop, Xatom.ATOM, 32, atoms, onerror=_swallow)
                ok = True
            elif target in self._text:
                self._put(requestor, prop, target, 8, self._secret)
                with self.lock:
                    self.served_at = time.monotonic()
                ok = True
            elif target == self._hint:
                requestor.change_property(prop, Xatom.STRING, 8, b"secret", onerror=_swallow)
                ok = True
        elif self._mode == "restored":
            if target == self._targets:
                atoms = [self._targets, *self._content]
                requestor.change_property(prop, Xatom.ATOM, 32, atoms, onerror=_swallow)
                ok = True
            elif target in self._content:
                self._put(requestor, prop, *self._content[target])
                ok = True
        requestor.send_event(
            xevent.SelectionNotify(
                time=ev.time, requestor=requestor, selection=ev.selection, target=target,
                property=prop if ok else X.NONE),
            onerror=_swallow)
        self._d.flush()

    def _put(self, requestor: Any, prop: int, type_atom: int, fmt: int, data: bytes) -> None:
        if len(data) <= CHUNK_BYTES:
            requestor.change_property(prop, type_atom, fmt, data, onerror=_swallow)
            return
        # ICCCM INCR: announce the size, then hand over one piece each time the requestor
        # deletes the property.
        requestor.change_attributes(event_mask=X.PropertyChangeMask, onerror=_swallow)
        requestor.change_property(prop, self._incr_type, 32, [len(data)], onerror=_swallow)
        self._incr[(requestor.id, prop)] = (
            requestor, type_atom, fmt, chunks(data), time.monotonic() + INCR_EXPIRY_S)

    def _continue_incr(self, window_id: int, prop: int) -> None:
        transfer = self._incr.get((window_id, prop))
        if transfer is None:
            return
        requestor, type_atom, fmt, pieces, _ = transfer
        piece = pieces.pop(0)
        requestor.change_property(prop, type_atom, fmt, piece, onerror=_swallow)
        if not pieces:
            del self._incr[(window_id, prop)]
        else:
            self._incr[(window_id, prop)] = (
                requestor, type_atom, fmt, pieces, time.monotonic() + INCR_EXPIRY_S)
        self._d.flush()

    def _expire_incr(self) -> None:
        now = time.monotonic()
        for key in [k for k, t in self._incr.items() if t[4] < now]:
            del self._incr[key]

    # ------------------------------------------------------------------ commands

    def _owner_id(self) -> int:
        owner = self._d.get_selection_owner(self._sel)
        return int(getattr(owner, "id", owner))  # a Window, or X.NONE

    def _owns(self) -> bool:
        return self._owner_id() == self._win.id

    def save(self) -> Content | None:
        owner = self._owner_id()
        if owner == X.NONE:
            return {}
        if owner == self._win.id:
            return dict(self._content) if self._mode == "restored" else None
        deadline = time.monotonic() + SAVE_BUDGET_S
        try:
            listed = self._convert(self._targets, deadline)
            if listed is None or listed[1] != 32:
                return None
            atoms = struct.unpack(f"={len(listed[2]) // 4}I", listed[2])
            wanted = [a for a in dict.fromkeys(atoms) if a not in self._skipped]
            content: Content = {}
            total = 0
            for target in wanted:
                got = self._convert(target, deadline, MAX_SNAPSHOT_BYTES - total)
                if got is None:
                    continue  # the owner lists it and refuses it: nobody could paste it
                total += len(got[2])
                if total > MAX_SNAPSHOT_BYTES:
                    return None
                content[target] = got
        except _NotRestorable:
            return None
        if wanted and not content:
            return None  # the owner hands over nothing at all
        return content

    def _convert(
        self, target: int, deadline: float, room: int = MAX_SNAPSHOT_BYTES,
    ) -> tuple[int, int, bytes] | None:
        """One representation of the selection: (type, format, data), None if refused."""
        self._win.delete_property(self._prop)
        self._win.convert_selection(self._sel, target, self._prop, X.CurrentTime)
        self._d.flush()
        step = min(deadline, time.monotonic() + CONVERT_TIMEOUT_S)
        ev = self._wait_for(
            lambda e: e.type == X.SelectionNotify and e.requestor.id == self._win.id
            and e.selection == self._sel and e.target == target, step)
        if ev.property == X.NONE:
            return None
        reply = self._win.get_property(
            self._prop, X.AnyPropertyType, 0, _MAX_PROPERTY_LEN, delete=True)
        if reply is None:
            return None
        if reply.property_type != self._incr_type:
            return reply.property_type, reply.format, _as_bytes(reply.value)
        # INCR: deleting the property above started the transfer; every further piece
        # arrives as a new value of the same property, an empty one ends it.
        pieces: list[bytes] = []
        size, type_atom, fmt = 0, 0, 8
        while True:
            step = min(deadline, time.monotonic() + CONVERT_TIMEOUT_S)
            self._wait_for(
                lambda e: e.type == X.PropertyNotify and e.window.id == self._win.id
                and e.atom == self._prop and e.state == X.PropertyNewValue, step)
            reply = self._win.get_property(
                self._prop, X.AnyPropertyType, 0, _MAX_PROPERTY_LEN, delete=True)
            piece = b"" if reply is None else _as_bytes(reply.value)
            if not piece:
                return type_atom, fmt, b"".join(pieces)
            type_atom, fmt = reply.property_type, reply.format
            pieces.append(piece)
            size += len(piece)
            if size > room:
                raise _NotRestorable

    def set_secret(self, text: str, target_window: int) -> None:
        self._forget()
        self._mode = "secret"
        self._secret = text.encode("utf-8")
        self._client = target_window  # 0 matches nobody
        with self.lock:
            self.secret_up = True
            self.served_at = None
            self.lost = False
        self._win.set_selection_owner(self._sel, X.CurrentTime)
        self._d.sync()
        if not self._owns():
            self._forget()
            raise RuntimeError("could not own the selection")

    def restore(self, content: Content) -> None:
        with self.lock:
            lost = self.lost
        if lost:
            # Something was copied in the meantime; that newer content stays.
            self._forget()
            return
        self._forget()
        if not content:
            if self._owns():  # the clipboard was empty before: make it empty again
                xrequest.SetSelectionOwner(
                    display=self._d.display, window=X.NONE, selection=self._sel,
                    time=X.CurrentTime)
                self._d.sync()
            return
        self._mode = "restored"
        self._content = dict(content)
        # Setting the owner again tells XFIXES listeners that the content changed.
        self._win.set_selection_owner(self._sel, X.CurrentTime)
        self._d.sync()


class XlibClipboard:
    """ClipboardPort over `_Owner`. `selection` is CLIPBOARD except in tests."""

    def __init__(self, selection: str = "CLIPBOARD", display_name: str | None = None) -> None:
        self._owner = _Owner(selection, display_name)  # None: the display in $DISPLAY
        self._owner.start()

    def close(self) -> None:
        self._owner.stop()

    def save(self) -> ClipSnapshot | None:
        try:
            content = self._owner.call(self._owner.save)
        except TimeoutError:
            return None
        return None if content is None else ClipSnapshot(content)

    def set_secret_text(self, text: str, target_window: int) -> None:
        self._owner.call(lambda: self._owner.set_secret(text, target_window))

    def restore(self, snap: ClipSnapshot) -> None:
        # The application reads the selection some time after Ctrl+V (up to 84 ms, and
        # Chrome reads it two or three times, S4.3): wait until its last read is 100 ms old.
        deadline = time.monotonic() + PASTE_TIMEOUT_S
        while time.monotonic() < deadline:
            with self._owner.lock:
                up, served_at = self._owner.secret_up, self._owner.served_at
            if not up or (served_at is not None and time.monotonic() - served_at >= PASTE_GRACE_S):
                break
            time.sleep(0.005)
        else:
            log_event("clipboard_paste_not_read")
        content = snap.payload
        assert isinstance(content, dict)
        self._owner.call(lambda: self._owner.restore(content))
