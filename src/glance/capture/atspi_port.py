"""AT-SPI adapter for the capture ports (needs a live X session and accessibility bus).

Facts this module encodes, from docs/superpowers/spike/findings.md:

- S2.1/S3.1: text and caret events of a rich editor come from an unfocused child block of
  the focused editable, so an event's owner is found by ancestry, never by FOCUSED on the
  source; the text is read in the block that holds the caret.
- S2.3: one `get_attributes()` call on a Chromium/Electron frame switches its tree on.
- S2.4: the page URL is the document attribute `URI` (Chromium) or `DocURL` (Gecko).
- S2.7: the private-window marker is only in the AT-SPI frame name, so that is the title.
- S3.1: a Python index into the raw `get_text` string equals the AT-SPI offset in both
  engines; offsets are converted to indices into the filler-free text in `_AtspiField` only.
- Some libatspi calls swallow a timeout and return an empty value (count 0, caret -1,
  name "", state `defunct`); `get_text`, `get_role_name` and the extents calls raise. The
  read order below makes a raising call follow every swallowing one.
"""
from __future__ import annotations

import os
import time
import zlib
from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Atspi", "2.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Wnck", "3.0")
from gi.repository import Atspi, Gdk, GLib, Wnck

from glance.capture.ports import (
    A11yField,
    A11yTimeout,
    FieldRead,
    clean_text,
    is_password_input,
    to_clean,
    to_raw,
)
from glance.log import log_event
from glance.privacy.gate import FieldMeta
from glance.protocol import Rect

TIMEOUT_MS = 200
GEOMETRY_SETTLE_MS = 120  # after the last move/resize of the field's window
# visible-data-changed: the field scrolled (GTK and others), so its caret moved on screen.
EVENTS = ("object:state-changed:focused", "object:text-changed", "object:text-caret-moved",
          "object:visible-data-changed")
EMBEDDED = "￼"
_SCREEN = Atspi.CoordType.SCREEN
_MAX_ANCESTORS = 64      # field -> application
_MAX_EVENT_HOPS = 16     # event source -> owning field
_MAX_LEAF_DEPTH = 8      # focused root -> block holding the caret
_MAX_MEMBERS = 256
_MAX_IGNORED = 256
_SEARCH_BUDGET_S = 0.15  # focused(): tree walk when no focus event has been seen
_SEARCH_MAX_NODES = 500
_APP_MISS_TTL_S = 2.0
# Embedded objects that are part of a line, not a block of the editor (findings S2.5).
_INLINE_ROLES = frozenset({
    Atspi.Role.LINK, Atspi.Role.IMAGE, Atspi.Role.STATIC, Atspi.Role.PUSH_BUTTON,
})


def _call(fn: Callable[..., Any], *args: Any) -> Any:
    """A call the field cannot do without: any failure makes the field unusable."""
    try:
        return fn(*args)
    except GLib.Error as e:
        raise A11yTimeout(getattr(fn, "__name__", "call")) from e


def _is_timeout(err: GLib.Error) -> bool:
    msg = (err.message or "").casefold()
    return "timeout" in msg or "timed out" in msg or "noreply" in msg


def _opt(fn: Callable[..., Any], *args: Any) -> Any:
    """A call whose result is optional: None when unsupported, A11yTimeout on a timeout."""
    try:
        return fn(*args)
    except GLib.Error as e:
        if _is_timeout(e):
            raise A11yTimeout(getattr(fn, "__name__", "call")) from e
        return None


def _quiet(fn: Callable[..., Any], *args: Any) -> Any:
    """For event handlers and searches: None on any failure."""
    try:
        return fn(*args)
    except GLib.Error:
        return None


def _state_nicks(obj: Atspi.Accessible) -> frozenset[str]:
    state_set = _call(obj.get_state_set)
    return frozenset(s.value_nick for s in state_set.get_states()) if state_set else frozenset()


def _has_text(obj: Atspi.Accessible) -> bool:
    return "Text" in (_quiet(obj.get_interfaces) or ())


def _char_count(obj: Atspi.Accessible) -> int:
    return Atspi.Text.get_character_count(obj)


def _rect(r: Any) -> Rect | None:
    """A usable rectangle, or None for the toolkits' "no rectangle" values.

    Height 0 means none (Chromium returns all zeros at the end of the text, S2.2); width 0
    with a real height is a valid GTK caret (S1.1).
    """
    if r is None or r.height <= 0 or r.width < 0:
        return None
    return Rect(int(r.x), int(r.y), int(r.width), int(r.height))


def pick_window(active: Any, stacked: list[Any], pid: int) -> Any:
    """The window of process `pid`: the active one if it is theirs, else their topmost."""
    if not pid:
        return None
    if active is not None and active.get_pid() == pid:
        return active
    for win in reversed(stacked):
        if win.get_pid() == pid:
            return win
    return None


class _AtspiField:
    """A11yField over the focused text accessible (`obj`, the root of a rich editor)."""

    def __init__(self, port: AtspiPort, obj: Atspi.Accessible) -> None:
        self._port = port
        self.obj = obj
        self.members: set[Atspi.Accessible] = set()  # descendants known to belong to obj
        self.pid = int(_call(obj.get_process_id))
        self._app = _call(obj.get_application)
        path, self._frame, self._doc = self._ancestry(obj)
        ident = _quiet(obj.get_id)
        digest = zlib.crc32(",".join(map(str, path)).encode())
        self.key = f"{self.pid}:{-1 if ident is None else ident}:{digest:08x}"

    @staticmethod
    def _ancestry(
        obj: Atspi.Accessible,
    ) -> tuple[list[int], Atspi.Accessible, Atspi.Accessible | None]:
        """Index path to the application, the top-level frame, the outermost web document."""
        path: list[int] = []
        frame: Atspi.Accessible | None = None
        doc: Atspi.Accessible | None = None
        node = obj
        for _ in range(_MAX_ANCESTORS):
            role = _call(node.get_role)
            if role == Atspi.Role.APPLICATION:
                if frame is None:
                    break
                return path, frame, doc
            if role == Atspi.Role.DOCUMENT_WEB:
                doc = node  # keeps the outermost one: iframes are nested documents (S2.4)
            path.append(int(_call(node.get_index_in_parent)))
            frame = node
            node = _call(node.get_parent)
            if node is None:
                break
        raise A11yTimeout("ancestry")  # detached or absurdly deep: not a usable field

    def _window(self) -> Wnck.Window:
        win = self._port.window_for(self.pid)
        if win is None:
            # No window carries this pid (a sandboxed app reports another one): there is no
            # class to gate on, and the active window's would be someone else's. Fail closed.
            raise A11yTimeout("no window")
        return win

    def meta(self) -> FieldMeta:
        states = _state_nicks(self.obj)
        if "defunct" in states:
            raise A11yTimeout("defunct")
        role = _call(self.obj.get_role_name) or ""
        if is_password_input(_opt(self.obj.get_attributes)):
            states = states | {"protected"}  # S2.6: second check next to the role
        win = self._window()
        wm_class = win.get_class_group_name() or win.get_class_instance_name() or ""
        if not wm_class:
            raise A11yTimeout("no window class")
        url: str | None = None
        # The frame name is the only place a private-window marker appears (S2.7), and
        # libatspi returns "" for it on a timeout. An empty name is passed on as it is, never
        # replaced by the X title, so that whoever gates on the title fails closed.
        title = _call(self._frame.get_name) or ""
        if self._doc is not None:
            # "" (not None) when the document has no readable URL: still web content.
            get = Atspi.Document.get_document_attribute_value
            url = _opt(get, self._doc, "URI") or _opt(get, self._doc, "DocURL") or ""
        return FieldMeta(
            app=_opt(self._app.get_name) or "",
            wm_class=wm_class,
            title=title,
            url=url,
            role=role,
            states=states,
            pid=self.pid,
            text_len=max(0, int(_call(_char_count, self.obj))),
        )

    def window_rect(self) -> Rect | None:
        win = self._port.window_for(self.pid)
        if win is None:
            return None
        x, y, w, h = win.get_geometry()
        return Rect(int(x), int(y), int(w), int(h))

    def _raw_text(self, node: Atspi.Accessible) -> str:
        count = int(_call(Atspi.Text.get_character_count, node))
        return _call(Atspi.Text.get_text, node, 0, max(0, count)) or ""

    def _leaf(self) -> Atspi.Accessible:
        """The block that holds the caret: follow embedded blocks at the caret offset."""
        node = self.obj
        # An empty get_text is the cheapest call that raises on a hung application; the
        # caret and count calls below would each swallow the timeout first.
        _call(Atspi.Text.get_text, node, 0, 0)
        for _ in range(_MAX_LEAF_DEPTH):
            caret = int(_call(Atspi.Text.get_caret_offset, node))
            if caret < 0 or _opt(Atspi.Text.get_text, node, caret, caret + 1) != EMBEDDED:
                break
            index = _opt(Atspi.Hypertext.get_link_index, node, caret)
            has_link = index is not None and index >= 0
            link = _opt(Atspi.Hypertext.get_link, node, index) if has_link else None
            child = _opt(link.get_object, 0) if link is not None else None
            if child is None or not _has_text(child):
                break
            if _opt(child.get_role) in _INLINE_ROLES:
                break
            if int(_call(Atspi.Text.get_caret_offset, child)) < 0:
                break
            node = child
        if node is not self.obj and len(self.members) < _MAX_MEMBERS:
            self.members.add(node)
        return node

    def read(self) -> FieldRead:
        leaf = self._leaf()
        raw = self._raw_text(leaf)  # raises on a timeout; see the module docstring
        caret = int(_call(Atspi.Text.get_caret_offset, leaf))
        caret_rect = prev_rect = None
        if 0 <= caret <= len(raw):
            extents = Atspi.Text.get_character_extents
            caret_rect = _rect(_opt(extents, leaf, caret, _SCREEN))
            if caret > 0:
                prev_rect = _rect(_opt(extents, leaf, caret - 1, _SCREEN))
        field_rect = _rect(_opt(Atspi.Component.get_extents, self.obj, _SCREEN))
        if caret_rect is not None and caret_rect == field_rect:
            caret_rect = None  # Gecko's answer for an empty textarea (S3.1)
        selection: tuple[int, int] | None = None
        if (_opt(Atspi.Text.get_n_selections, leaf) or 0) > 0:
            sel = _opt(Atspi.Text.get_selection, leaf, 0)
            if sel is not None and sel.start_offset != sel.end_offset:
                lo, hi = sorted((sel.start_offset, sel.end_offset))
                selection = (to_clean(raw, lo), to_clean(raw, hi))
        return FieldRead(
            text=clean_text(raw),
            caret=to_clean(raw, caret),
            selection=selection,
            caret_rect=caret_rect,
            prev_char_rect=prev_rect,
            field_rect=field_rect,
            window_rect=self.window_rect(),
        )

    def set_selection(self, start: int, end: int) -> bool:
        leaf = self._leaf()
        raw = self._raw_text(leaf)
        lo, hi = to_raw(raw, start), to_raw(raw, end)
        ok = False
        if (_opt(Atspi.Text.get_n_selections, leaf) or 0) > 0:
            ok = bool(_opt(Atspi.Text.set_selection, leaf, 0, lo, hi))
        return ok or bool(_opt(Atspi.Text.add_selection, leaf, lo, hi))

    def set_caret(self, offset: int) -> bool:
        leaf = self._leaf()
        raw = self._raw_text(leaf)
        return bool(_opt(Atspi.Text.set_caret_offset, leaf, to_raw(raw, offset)))


class AtspiPort:
    """A11yPort over AT-SPI events, with Wnck for the window a field belongs to."""

    def __init__(self) -> None:
        # No client-side cache: role, states and names must be the application's current
        # answer, since the privacy gate is evaluated on them.
        os.environ.setdefault("ATSPI_NO_CACHE", "1")
        Gdk.init([])
        Atspi.init()
        Atspi.set_timeout(TIMEOUT_MS, TIMEOUT_MS)
        self._screen = Wnck.Screen.get_default()
        if self._screen is None:
            raise RuntimeError("no X11 screen")
        self._screen.force_update()
        self._on_focus: Callable[[A11yField | None], None] | None = None
        self._on_change: Callable[[A11yField], None] | None = None
        self._listener: Atspi.EventListener | None = None
        self._current: _AtspiField | None = None
        self._pending: Atspi.Accessible | None = None  # focused before its window was active
        self._ignored: set[Atspi.Accessible] = set()   # event sources known to be irrelevant
        self._apps: dict[int, Atspi.Accessible] = {}
        self._app_miss: dict[int, float] = {}  # pid -> time before which not to rescan
        self._followed: Wnck.Window | None = None  # the active window, for its geometry
        self._follow_id = 0
        self._settle_id = 0

    # ------------------------------------------------------------------ A11yPort

    def start(
        self,
        on_focus: Callable[[A11yField | None], None],
        on_change: Callable[[A11yField], None],
    ) -> None:
        self._on_focus = on_focus
        self._on_change = on_change
        self._listener = Atspi.EventListener.new(self._on_event)
        for name in EVENTS:
            self._listener.register(name)
        self._screen.connect("active-window-changed", self._on_active_window)
        self._follow_active()
        self._wake_active_tree()

    def reregister(self) -> None:
        """Register every AT-SPI listener again and forget what was cached about apps.

        After a suspend the accessibility bus may have restarted or dropped our
        registrations; asking again is cheap and idempotent."""
        if self._listener is None:
            return
        for name in EVENTS:
            _quiet(self._listener.deregister, name)
            self._listener.register(name)
        self._apps.clear()
        self._app_miss.clear()
        self._ignored.clear()
        self._current = None
        self._pending = None
        self._wake_active_tree()

    def stop(self) -> None:
        if self._listener is not None:
            for name in EVENTS:
                _quiet(self._listener.deregister, name)
            self._listener = None

    def focused(self) -> A11yField | None:
        if self._current is None:
            obj = self._search_focused()
            if obj is not None:
                self._current = self._make_field(obj)
        return self._current

    # ------------------------------------------------------------------ windows

    def _active_pid(self) -> int:
        win = self._screen.get_active_window()
        return int(win.get_pid()) if win is not None else 0

    def _in_active_window(self, pid: int) -> bool:
        """True when the active window belongs to process `pid`."""
        return pid != 0 and pid == self._active_pid()

    def window_for(self, pid: int) -> Wnck.Window | None:
        """The window of process `pid` to take class and geometry from, or None.

        Never another process's window: a field gated against the wrong window class could
        pass as the wrong application.
        """
        return pick_window(
            self._screen.get_active_window(), self._screen.get_windows_stacked(), pid)

    def _app_for(self, pid: int) -> Atspi.Accessible | None:
        now = time.monotonic()
        if pid and pid not in self._apps and now >= self._app_miss.get(pid, 0.0):
            self._apps.clear()
            desktop = Atspi.get_desktop(0)
            for i in range(_quiet(desktop.get_child_count) or 0):
                app = _quiet(desktop.get_child_at_index, i)
                app_pid = _quiet(app.get_process_id) if app is not None else None
                if app_pid:
                    self._apps.setdefault(int(app_pid), app)
            if pid not in self._apps:
                # Not on the bus (yet): do not rescan the desktop on every event meanwhile.
                if len(self._app_miss) >= 64:
                    self._app_miss.clear()
                self._app_miss[pid] = now + _APP_MISS_TTL_S
        return self._apps.get(pid)

    def _wake_active_tree(self) -> None:
        """S2.3: asking a Chromium/Electron frame for its attributes switches its tree on."""
        app = self._app_for(self._active_pid())
        if app is None:
            return
        for i in range(min(_quiet(app.get_child_count) or 0, 32)):
            frame = _quiet(app.get_child_at_index, i)
            if frame is not None:
                _quiet(frame.get_attributes)

    def _follow_active(self) -> None:
        """Watch the active window's geometry: when it moves or resizes, every rectangle of
        the field in it is somewhere else on screen."""
        win = self._screen.get_active_window()
        if win is self._followed:
            return
        if self._followed is not None and self._follow_id:
            _quiet(self._followed.disconnect, self._follow_id)
        self._followed = win
        self._follow_id = win.connect("geometry-changed", self._on_geometry) \
            if win is not None else 0

    def _on_geometry(self, win: Wnck.Window) -> None:
        try:
            cur = self._current
            if cur is not None and self._on_change is not None and cur.pid \
                    and cur.pid == int(win.get_pid()):
                self._on_change(cur)  # a fresh snapshot, with the new screen coordinates
                # The app lays itself out again a moment after a resize: read once more
                # when the window has been still for GEOMETRY_SETTLE_MS.
                if getattr(self, "_settle_id", 0):
                    GLib.source_remove(self._settle_id)
                self._settle_id = GLib.timeout_add(GEOMETRY_SETTLE_MS, self._geometry_settled)
        except Exception as e:  # noqa: BLE001 - a signal handler must not raise into GLib
            log_event("atspi_window_error", kind=type(e).__name__)

    def _geometry_settled(self) -> bool:
        self._settle_id = 0
        cur = self._current
        if cur is not None and self._on_change is not None:
            self._on_change(cur)
        return False

    def _on_active_window(self, _screen: Wnck.Screen, _previous: Wnck.Window | None) -> None:
        try:
            self._follow_active()
            self._wake_active_tree()
            cur = self._current
            if cur is not None and not self._in_active_window(cur.pid):
                self._set_focus(None)
            pending, self._pending = self._pending, None
            if pending is not None and self._current is None:
                pid = _quiet(pending.get_process_id)
                if pid and int(pid) == self._active_pid() and self._is_focused_text(pending):
                    self._set_focus(pending)
        except Exception as e:  # noqa: BLE001 - a signal handler must not raise into GLib
            log_event("atspi_window_error", kind=type(e).__name__)

    # ------------------------------------------------------------------ focus

    def _make_field(self, obj: Atspi.Accessible) -> _AtspiField | None:
        try:
            return _AtspiField(self, obj)
        except A11yTimeout:
            return None

    def _set_focus(self, obj: Atspi.Accessible | None) -> _AtspiField | None:
        assert self._on_focus is not None
        self._ignored.clear()
        if obj is not None:
            cur = self._current
            field = cur if cur is not None and cur.obj == obj else self._make_field(obj)
            if field is not None:
                self._current = field
                self._on_focus(field)
                return field
        if self._current is not None:
            self._current = None
            self._on_focus(None)
        return None

    def _is_focused_text(self, obj: Atspi.Accessible) -> bool:
        state_set = _quiet(obj.get_state_set)
        return bool(
            state_set is not None
            and state_set.contains(Atspi.StateType.FOCUSED)
            and _has_text(obj)
        )

    def _adopt(self, obj: Atspi.Accessible) -> _AtspiField | None:
        """Make `obj` the current field if its window is the active one; else remember it."""
        pid = _quiet(obj.get_process_id)
        if not pid:
            return None
        if not self._in_active_window(int(pid)):
            # Either a background application, or the window manager has not reported the
            # switch yet; `_on_active_window` picks it up in the second case.
            self._pending = obj
            return None
        return self._set_focus(obj)

    def _on_event(self, event: Atspi.Event) -> None:
        try:
            src = event.source
            if src is None or self._on_focus is None:
                return
            if event.type.startswith("object:state-changed:focused"):
                self._focus_event(src, bool(event.detail1))
            else:
                self._text_event(src)
        except A11yTimeout:
            pass
        except Exception as e:  # noqa: BLE001 - a listener must not raise into libatspi
            log_event("atspi_event_error", kind=type(e).__name__)

    def _focus_event(self, src: Atspi.Accessible, gained: bool) -> None:
        cur = self._current
        if not gained:
            if cur is not None and src == cur.obj:
                self._set_focus(None)
            return
        if _has_text(src):
            self._adopt(src)
        elif cur is not None:
            pid = _quiet(src.get_process_id)
            if pid and self._in_active_window(int(pid)):
                self._set_focus(None)  # focus moved to something that is not a text field

    def _text_event(self, src: Atspi.Accessible) -> None:
        assert self._on_change is not None
        cur = self._current
        if cur is not None and (src == cur.obj or src in cur.members):
            self._on_change(cur)
            return
        if src in self._ignored:
            return
        chain: list[Atspi.Accessible] = []
        node: Atspi.Accessible | None = src
        for _ in range(_MAX_EVENT_HOPS):
            if node is None:
                break
            if cur is not None and node == cur.obj:
                if len(cur.members) + len(chain) <= _MAX_MEMBERS:
                    cur.members.update(chain)
                self._on_change(cur)
                return
            chain.append(node)
            node = _quiet(node.get_parent)
        # Not under the current field. A focus event may have been missed (a Chromium tree
        # switched on after the field was focused sends none): look for the focused owner.
        for i, node in enumerate(chain):
            if self._is_focused_text(node):
                field = self._adopt(node)
                if field is not None:
                    field.members.update(chain[:i])
                return
        if len(self._ignored) >= _MAX_IGNORED:
            self._ignored.clear()
        self._ignored.add(src)

    # ------------------------------------------------------------------ search

    def _search_focused(self) -> Atspi.Accessible | None:
        """The focused text object of the active window, by a bounded tree walk."""
        app = self._app_for(self._active_pid())
        if app is None:
            return None
        deadline = time.monotonic() + _SEARCH_BUDGET_S
        stack: list[Atspi.Accessible] = []
        for i in range(min(_quiet(app.get_child_count) or 0, 32)):
            frame = _quiet(app.get_child_at_index, i)
            state_set = _quiet(frame.get_state_set) if frame is not None else None
            if state_set is not None and state_set.contains(Atspi.StateType.ACTIVE):
                stack.append(frame)
        seen = 0
        while stack and seen < _SEARCH_MAX_NODES and time.monotonic() < deadline:
            node = stack.pop()
            seen += 1
            if self._is_focused_text(node):
                return node
            for i in range(min(_quiet(node.get_child_count) or 0, 100)):
                child = _quiet(node.get_child_at_index, i)
                if child is not None:
                    stack.append(child)
        return None
