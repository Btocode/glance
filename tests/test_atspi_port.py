"""Port logic that needs no desktop: stub accessibles and windows stand in for AT-SPI/Wnck."""
from types import SimpleNamespace

import pytest

pytest.importorskip("gi")

from glance.capture import atspi_port
from glance.capture.atspi_port import AtspiPort, _AtspiField, _rect, pick_window
from glance.capture.ports import A11yTimeout
from glance.config import Config
from glance.privacy.gate import PrivacyGate
from glance.protocol import Rect

Role = atspi_port.Atspi.Role


class Node:
    """Stub accessible."""

    def __init__(self, role, name="", parent=None, index=0, pid=4242,
                 states=("editable", "focused"), role_name="entry", attrs=None,
                 interfaces=("Accessible", "Text")):
        self._role, self._name, self._parent, self._index = role, name, parent, index
        self._pid, self._states, self._role_name = pid, states, role_name
        self._attrs, self._interfaces = attrs or {}, interfaces
        self.app = None

    def get_role(self): return self._role
    def get_role_name(self): return self._role_name
    def get_name(self): return self._name
    def get_parent(self): return self._parent
    def get_index_in_parent(self): return self._index
    def get_process_id(self): return self._pid
    def get_id(self): return 7
    def get_attributes(self): return self._attrs
    def get_interfaces(self): return list(self._interfaces)
    def get_application(self): return self.app

    def get_state_set(self):
        return SimpleNamespace(
            get_states=lambda: [SimpleNamespace(value_nick=s) for s in self._states],
            contains=lambda state: state.value_nick in self._states,
        )


class Win:
    def __init__(self, pid, cls="Google-chrome", name="Inbox - Google Chrome"):
        self._pid, self._cls, self._name = pid, cls, name

    def get_pid(self): return self._pid
    def get_class_group_name(self): return self._cls
    def get_class_instance_name(self): return self._cls.lower()
    def get_name(self): return self._name
    def get_geometry(self): return (10, 20, 800, 600)


class Screen:
    def __init__(self, active, windows):
        self.active, self.windows = active, windows

    def get_active_window(self): return self.active
    def get_windows_stacked(self): return list(self.windows)
    def get_windows(self): return list(self.windows)


def make_port(screen) -> AtspiPort:
    port = AtspiPort.__new__(AtspiPort)  # no X display, no bus
    port._screen = screen
    port._current = port._pending = None
    port._ignored, port._apps, port._app_miss = set(), {}, {}
    port.focus_calls = []
    port._on_focus = port.focus_calls.append
    port._on_change = lambda field: None
    return port


def make_tree(frame_name: str, pid: int = 4242) -> Node:
    """application > frame > field, with no web document (browser UI such as the URL bar)."""
    app = Node(Role.APPLICATION, name="Google Chrome", pid=pid)
    frame = Node(Role.FRAME, name=frame_name, parent=app, pid=pid)
    field = Node(Role.ENTRY, parent=frame, index=3, pid=pid)
    field.app = app
    return field


@pytest.fixture(autouse=True)
def char_count(monkeypatch):
    monkeypatch.setattr(atspi_port, "_char_count", lambda obj: 12)


# ---------------------------------------------------------------- _rect

@pytest.mark.parametrize("box, expected", [
    ((389, 270, 0, 24), Rect(389, 270, 0, 24)),   # GTK caret at end of text: width 0 is valid
    ((490, 619, 4, 17), Rect(490, 619, 4, 17)),
    ((-1900, -30, 6, 19), Rect(-1900, -30, 6, 19)),  # monitor with a negative origin
    ((0, 0, 0, 0), None),                         # Chromium at end of text
    ((-1, -1, 0, 0), None),
    ((10, 10, 5, -3), None),
    ((10, 10, -1, 17), None),
])
def test_rect_normalises_toolkit_garbage(box, expected):
    x, y, w, h = box
    assert _rect(SimpleNamespace(x=x, y=y, width=w, height=h)) == expected


def test_rect_none():
    assert _rect(None) is None


# ---------------------------------------------------------------- windows

def test_pick_window_prefers_the_active_window_of_the_process():
    mine, mine_bg, other = Win(1), Win(1), Win(2)
    assert pick_window(mine, [mine_bg, other, mine], 1) is mine


def test_pick_window_takes_the_process_topmost_window_when_another_app_is_active():
    low, top, other = Win(1), Win(1), Win(2)
    assert pick_window(other, [low, top, other], 1) is top


def test_pick_window_never_returns_another_process_window():
    other = Win(2, cls="gedit")
    assert pick_window(other, [other], 1) is None
    assert pick_window(other, [other], 0) is None
    assert pick_window(None, [], 1) is None


def test_process_without_a_window_is_not_active():
    port = make_port(Screen(Win(2), [Win(2)]))
    assert port._in_active_window(2) is True
    assert port._in_active_window(4242) is False
    assert make_port(Screen(None, []))._in_active_window(0) is False


def test_field_of_a_process_without_a_window_fails_closed():
    # A sandboxed password manager: AT-SPI pid 4242, its X window says pid 2.
    port = make_port(Screen(Win(2, cls="gedit"), [Win(2, cls="gedit")]))
    field = _AtspiField(port, make_tree("Vault"))
    with pytest.raises(A11yTimeout):
        field.meta()
    assert field.window_rect() is None


def test_background_text_event_from_windowless_process_is_not_adopted():
    port = make_port(Screen(Win(2, cls="gedit"), [Win(2, cls="gedit")]))
    src = make_tree("Vault")
    port._text_event(src)
    assert port._current is None and port.focus_calls == []
    assert port._pending is src  # picked up only if its window becomes the active one


def test_focus_event_in_the_active_window_is_adopted():
    port = make_port(Screen(Win(4242), [Win(4242)]))
    src = make_tree("Inbox - Google Chrome")
    port._focus_event(src, True)
    assert port._current is not None and port._current.obj is src
    assert port.focus_calls == [port._current]


# ---------------------------------------------------------------- title

def test_frame_name_is_the_title():
    port = make_port(Screen(Win(4242, name="Inbox - Google Chrome"), [Win(4242)]))
    meta = _AtspiField(port, make_tree("Inbox - Google Chrome (Incognito)")).meta()
    assert meta.title == "Inbox - Google Chrome (Incognito)"
    assert (meta.wm_class, meta.url, meta.pid, meta.text_len) == ("Google-chrome", None, 4242, 12)
    assert PrivacyGate(Config()).evaluate(meta).reason == "incognito"


@pytest.mark.parametrize("cls", ["Google-chrome", "Slack", "gedit"])
def test_empty_frame_name_is_never_replaced_by_the_x_title(cls):
    # libatspi answers "" for a frame name it could not read. The X title has no
    # private-window marker (S2.7), so it must not stand in for the frame name.
    win = Win(4242, cls=cls, name="Secret - Google Chrome")
    port = make_port(Screen(win, [win]))
    meta = _AtspiField(port, make_tree("")).meta()
    assert meta.title == ""


def test_browser_ui_field_with_unreadable_frame_name_is_gated():
    win = Win(4242, cls="Google-chrome", name="Secret - Google Chrome")
    port = make_port(Screen(win, [win]))
    meta = _AtspiField(port, make_tree("")).meta()
    assert PrivacyGate(Config()).evaluate(meta).allowed is False


def test_password_attribute_sets_protected_state():
    win = Win(4242)
    port = make_port(Screen(win, [win]))
    src = make_tree("Sign in - Google Chrome")
    src._attrs = {"text-input-type": "password"}
    meta = _AtspiField(port, src).meta()
    assert "protected" in meta.states
    assert PrivacyGate(Config()).evaluate(meta).reason == "password"


def test_defunct_field_raises():
    win = Win(4242)
    port = make_port(Screen(win, [win]))
    src = make_tree("x")
    field = _AtspiField(port, src)
    src._states = ("defunct",)
    with pytest.raises(A11yTimeout):
        field.meta()


# ---------------------------------------------------------------- app lookup

def test_app_lookup_miss_is_cached_briefly(monkeypatch):
    scans = []

    def desktop(_index):
        scans.append(1)
        return SimpleNamespace(get_child_count=lambda: 0)

    clock = [100.0]
    monkeypatch.setattr(atspi_port.Atspi, "get_desktop", desktop)
    monkeypatch.setattr(atspi_port.time, "monotonic", lambda: clock[0])
    port = make_port(Screen(None, []))
    assert port._app_for(999) is None and port._app_for(999) is None
    assert len(scans) == 1
    clock[0] += atspi_port._APP_MISS_TTL_S + 0.1
    assert port._app_for(999) is None
    assert len(scans) == 2


class GeomWin:
    def __init__(self, pid):
        self.pid, self.handlers = pid, []

    def get_pid(self): return self.pid
    def connect(self, signal, cb):
        self.handlers.append((signal, cb))
        return len(self.handlers)
    def disconnect(self, handler_id): self.handlers[handler_id - 1] = None


def test_a_moved_or_resized_window_rereads_its_field():
    one, two = GeomWin(4242), GeomWin(99)
    screen = Screen(one, [one, two])
    port = make_port(screen)
    port._followed, port._follow_id = None, 0
    changed = []
    port._on_change = changed.append
    port._follow_active()
    (signal, cb), = one.handlers
    assert signal == "geometry-changed"
    port._current = SimpleNamespace(pid=4242)
    cb(one)
    assert changed == [port._current]
    screen.active = two  # another window is active now: the first one is let go
    port._follow_active()
    assert one.handlers == [None] and two.handlers[0][0] == "geometry-changed"
    two.handlers[0][1](two)  # not the field's window: nothing to re-read
    assert changed == [port._current]
    port._current = None
    cb(one)
    assert len(changed) == 1


# ---------------------------------------------------------------- Wayland coordinates


def wayland_field(monkeypatch, frame_size, doc_origin=None):
    """A field whose port reads WINDOW coordinates (GNOME Wayland), with stub extents."""
    from glance.capture.windows_shell import ShellWindow

    win = ShellWindow({"id": "1", "pid": 4242, "frame": [100, 50, 700, 500],
                       "buffer": [75, 25, 750, 550]})
    field = _AtspiField.__new__(_AtspiField)
    field.pid = 4242
    field._port = SimpleNamespace(window_coords=True, window_for=lambda pid: win)
    field._frame = "frame"
    field._doc = "doc" if doc_origin else None
    field._app = SimpleNamespace(get_toolkit_name=lambda: "", get_toolkit_version=lambda: "")
    boxes = {"frame": SimpleNamespace(x=0, y=0, width=frame_size[0], height=frame_size[1]),
             "doc": SimpleNamespace(x=(doc_origin or (0, 0))[0], y=(doc_origin or (0, 0))[1],
                                    width=600, height=400)}
    monkeypatch.setattr(atspi_port.Atspi.Component, "get_extents",
                        lambda obj, coords: boxes[obj])
    return field, win


def test_window_coordinates_start_at_the_buffer_or_the_frame(monkeypatch):
    field, _win = wayland_field(monkeypatch, (750, 550))  # GTK 3, Chromium: the buffer
    assert field._coords()[1:] == (75, 25)
    field, _win = wayland_field(monkeypatch, (700, 500))  # GTK 4: the frame
    assert field._coords()[1:] == (100, 50)


def test_chromium_caret_is_moved_by_the_document_origin_only_when_that_fits(monkeypatch):
    field, _win = wayland_field(monkeypatch, (750, 550), doc_origin=(12, 100))
    box = SimpleNamespace(x=52, y=140, width=500, height=96)
    caret = SimpleNamespace(x=42, y=42, width=1, height=16)  # relative to the document
    assert field._document_shift(atspi_port._WINDOW, caret, box) == (12, 100)
    inside = SimpleNamespace(x=60, y=150, width=1, height=16)  # already in the field
    assert field._document_shift(atspi_port._WINDOW, inside, box) == (0, 0)
    assert field._document_shift(atspi_port._SCREEN, caret, box) == (0, 0)  # X11
