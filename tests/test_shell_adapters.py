"""The GNOME Wayland adapters over a fake extension bus: no shell, no display."""
from __future__ import annotations

from typing import Any

import pytest

from glance.capture.windows_shell import ShellScreen
from glance.daemon.shell_link import dispatch
from glance.protocol import Rect
from glance.session import GNOME_WAYLAND, X11, is_gnome, session_kind
from glance.shellbus import DAEMON_XML, SHELL_XML, ShellUnavailable, decode, encode
from glance.ui import scene as sc
from glance.ui.hotkeys_shell import ShellHotkeys, to_accel
from glance.ui.overlay_shell import ShellOverlay
from glance.ui.panel_model import IssueRow, PanelView, ToneChip
from glance.ui.placement import Anchor
from glance.ui.selection_shell import ShellSelection
from glance.ui.status_model import StatusState, StatusView
from glance.ui.strip_model import Segment, StripView
from glance.ui.theme import LIGHT
from glance.writeback.clipboard_shell import ShellClipboard
from glance.writeback.keys_shell import ShellKeys, typeable
from glance.writeback.ports import ClipSnapshot, KeysAborted

WINDOW = {"id": "77", "pid": 4242, "wm_class": "org.gnome.TextEditor", "title": "Notes",
          "frame": [100, 100, 800, 600], "buffer": [90, 80, 820, 640]}


class FakeBus:
    def __init__(self, answers: dict[str, Any] | None = None, available: bool = True) -> None:
        self.answers = answers or {}
        self.available = available
        self.calls: list[tuple[str, Any]] = []
        self.signals: dict[str, Any] = {}

    def call(self, method: str, payload: Any = None, timeout_ms: int = 0) -> Any:
        if not self.available:
            raise ShellUnavailable(method)
        self.calls.append((method, payload))
        answer = self.answers.get(method)
        if isinstance(answer, Exception):
            raise answer
        return answer(payload) if callable(answer) else answer

    def subscribe(self, signal: str, fn: Any) -> None:
        self.signals[signal] = fn

    def methods(self) -> list[str]:
        return [m for m, _p in self.calls]


# ---------------------------------------------------------------- session


@pytest.mark.parametrize("env, kind", [
    ({"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}, X11),
    ({"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0",
      "XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}, GNOME_WAYLAND),
    ({"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0",
      "XDG_CURRENT_DESKTOP": "GNOME"}, GNOME_WAYLAND),
    ({"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0",
      "XDG_CURRENT_DESKTOP": "KDE"}, None),
    ({"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0",
      "XDG_CURRENT_DESKTOP": "sway"}, None),
    ({"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "GNOME"}, None),
    ({"XDG_SESSION_TYPE": "tty"}, None),
])
def test_session_kind(env, kind):
    assert session_kind(env) == kind


def test_is_gnome_accepts_gnome_classic_and_ubuntu():
    assert is_gnome({"XDG_CURRENT_DESKTOP": "GNOME-Classic:GNOME"})
    assert not is_gnome({"XDG_CURRENT_DESKTOP": "Unity"})


# ---------------------------------------------------------------- the bus


def test_interfaces_parse_and_payloads_round_trip():
    from gi.repository import Gio

    shell = Gio.DBusNodeInfo.new_for_xml(SHELL_XML).interfaces[0]
    assert shell.lookup_method("TypeText") is not None
    assert shell.lookup_signal("WindowChanged") is not None
    assert Gio.DBusNodeInfo.new_for_xml(DAEMON_XML).interfaces[0].lookup_method("Clicked")
    assert decode(encode({"text": "naïve ✓"})) == {"text": "naïve ✓"}
    assert decode("not json") is None


def test_dispatch_hands_the_decoded_payload_and_survives_a_bad_handler():
    seen = []
    handlers = {"Key": seen.append, "Clicked": lambda p: 1 / 0}
    assert dispatch(handlers, "Key", '{"name": "panel"}')
    assert seen == [{"name": "panel"}]
    assert dispatch(handlers, "Clicked", "{}")  # raised inside: logged, not propagated
    assert not dispatch(handlers, "Nope", "{}")
    assert dispatch(handlers, "Key", "[1]") and seen[-1] == {}


# ---------------------------------------------------------------- scene


def test_scene_serializes_views_and_escapes_the_text():
    view = StripView((Segment("a <b> ", False), Segment("it's", True)), 1)
    piece = sc.strip_piece(view, 1, 2, 300, LIGHT, "Alt+Return")
    assert piece["markup"] == 'a &lt;b&gt; <span foreground="#0B7A52" weight="600">it\'s</span>'
    assert piece["can_use"] and piece["use_label"] == sc.USE_LABEL == "Alt+Enter"
    assert piece["view"]["segments"][1] == {"text": "it's", "changed": True}
    assert sc.use_label("Ctrl+Return") == "Ctrl+Enter"
    suggested = sc.suggested_piece(view, LIGHT, "Alt+Return")
    assert suggested["markup"] == (f'a &lt;b&gt; <span background="{LIGHT["highlight"]}" '
                                   f'foreground="{LIGHT["text"]}">it\'s</span>')
    assert suggested["can_use"] and suggested["apply_label"] == "Apply (Alt+Enter)"


def test_panel_piece_marks_spelling_rows():
    from glance.grammar.harper import SPELL_RULE

    view = PanelView(issues=(IssueRow("i1", "teh cat", "Spelling", ("the",), SPELL_RULE, "teh"),
                             IssueRow("i2", "a apple", "Article", ("an",), "AnA", "a")),
                     tone_chips=(ToneChip("friendly", "Friendlier", 1),))
    piece = sc.panel_piece(view, 0, 0, 520, True)
    assert [r["spelling"] for r in piece["view"]["issues"]] == [True, False]
    assert piece["view"]["tone_chips"][0]["label"] == "Friendlier"
    assert piece["view"]["tone_chips"][0]["glyph"] == "heart"
    assert piece["preview_markup"] is None
    assert (piece["title"], piece["subline"], piece["fixes_label"]) == (
        "Glance Assistant", "Local grammar · AI tones", "2 fixes")
    scene = sc.scene(LIGHT, None, panel=piece)
    assert scene["look"]["glyphs"]["heart"][0] == ["M", 8, 13.2]
    assert scene["look"]["words"]["fix_all"] == "Fix All"


def test_rects_drop_junk():
    assert sc.rects([[0, 0, 10, 10], [1, 2], "x", [0, 0, 0, 5]]) == [Rect(0, 0, 10, 10)]


# ---------------------------------------------------------------- overlay


def make_overlay(bus: FakeBus):
    posted: list[Any] = []
    later: list[Any] = []
    clicks: list[Any] = []
    actions: list[Any] = []
    ov = ShellOverlay(bus, posted.append, lambda ms, fn: later.append((ms, fn)),
                      lambda: LIGHT, lambda: clicks.append(1),
                      lambda action, args: actions.append((action, args)))

    def run() -> None:
        while posted:
            posted.pop(0)()

    return ov, run, later, clicks, actions


ANCHOR = Anchor(caret=Rect(300, 200, 2, 18), field=Rect(200, 190, 400, 60),
                window=Rect(100, 100, 800, 600), pid=4242)


def bus_with_monitors() -> FakeBus:
    return FakeBus({"Monitors": [[0, 0, 1280, 800]], "Render": {"ok": True}})


def last_scene(bus: FakeBus) -> dict[str, Any]:
    return [p for m, p in bus.calls if m == "Render"][-1]


def test_changes_in_one_turn_are_one_render_with_placed_pieces():
    bus = bus_with_monitors()
    ov, run, *_ = make_overlay(bus)
    ov.on_window({"reason": "focus", "focus": WINDOW, "monitors": [[0, 0, 1280, 800]]})
    ov.show_status(StatusView(StatusState.READY, count=2), ANCHOR)
    ov.show_strip(StripView((Segment("Fixed.", True),), 1), ANCHOR)
    run()
    assert bus.methods().count("Render") == 1
    scene = last_scene(bus)
    assert scene["status"]["badge"] == "2" and scene["strip"]["w"] >= 320
    assert scene["window"] == {"id": "77", "frame": [100, 100, 800, 600]}
    assert scene["strip"]["y"] > 200  # under the caret line
    assert ov.strip_text() == "Fixed." and ov.status_geometry() is not None


def test_nothing_is_sent_without_the_extension():
    bus = FakeBus(available=False)
    ov, run, *_ = make_overlay(bus)
    ov.show_status(StatusView(StatusState.IDLE), ANCHOR)
    run()
    assert bus.calls == []


def test_a_field_in_another_process_window_is_not_followed():
    bus = bus_with_monitors()
    ov, run, *_ = make_overlay(bus)
    ov.on_window({"reason": "focus", "focus": {**WINDOW, "pid": 1}})
    ov.show_status(StatusView(StatusState.IDLE), ANCHOR)
    run()
    assert last_scene(bus)["window"] is None


def test_a_window_move_moves_the_anchor_and_a_resize_reshows_later():
    bus = bus_with_monitors()
    ov, run, later, *_ = make_overlay(bus)
    ov.on_window({"reason": "focus", "focus": WINDOW})
    ov.show_status(StatusView(StatusState.IDLE), ANCHOR)
    run()
    x0 = last_scene(bus)["status"]["x"]
    moved = {**WINDOW, "frame": [150, 120, 800, 600]}
    ov.on_window({"reason": "position", "focus": moved})
    ov.show_status(StatusView(StatusState.READY, count=1), ov._anchor)
    run()
    assert last_scene(bus)["status"]["x"] == x0 + 50
    renders = bus.methods().count("Render")
    ov.on_window({"reason": "size", "focus": {**moved, "frame": [150, 120, 900, 600]}})
    assert later and later[0][0] == 400
    later[0][1]()
    run()
    assert bus.methods().count("Render") == renders + 1


def test_the_open_panel_shows_the_strip_s_text_in_its_place():
    bus = bus_with_monitors()
    ov, run, *_ = make_overlay(bus)
    ov.on_window({"reason": "focus", "focus": WINDOW})
    ov.show_strip(StripView((Segment("Fixed.", True),), 1), ANCHOR)
    run()
    strip_y = last_scene(bus)["strip"]["y"]
    ov.show_panel(PanelView(), 0, 0, ANCHOR)
    run()
    scene = last_scene(bus)
    # the strip is not drawn on its own: the panel shows its text, where the strip was
    assert scene["strip"] is None and ov.strip_text() is None
    assert scene["panel"]["y"] == strip_y and scene["panel"]["follow"]
    assert scene["panel"]["suggested"]["markup"].endswith(">Fixed.</span>")
    assert scene["panel"]["w"] >= 520
    ov.hide_strip()
    ov.show_panel(PanelView(mode="selection"), 500, 400)
    run()
    scene = last_scene(bus)
    assert scene["panel"]["follow"] is False and scene["panel"]["x"] == 500


def test_reported_sizes_place_again():
    bus = bus_with_monitors()
    ov, run, *_ = make_overlay(bus)
    ov.show_strip(StripView((Segment("Fixed.", True),), 1), ANCHOR)
    run()
    ov.on_sized({"strip_h": 90})
    run()
    assert bus.methods().count("Render") == 2
    assert last_scene(bus)["sizes"]["strip_h"] == 90
    ov.on_sized({"strip_h": 90})
    run()
    assert bus.methods().count("Render") == 2


def test_clicks_and_the_rewrite_entry_reach_their_callbacks():
    bus = FakeBus({"Ask": {"ok": True}})
    ov, _run, _later, clicks, actions = make_overlay(bus)
    ov.on_clicked({"target": "status", "args": {}})
    ov.on_clicked({"target": "action", "args": {"action": "accept",
                                                 "args": {"issue_id": "i", "choice": 0}}})
    ov.on_clicked({"target": "action", "args": {"action": 3}})
    assert clicks == [1] and actions == [("accept", {"issue_id": "i", "choice": 0})]
    got = []
    ov.ask_text("Rewrite as…", lambda text, back: got.append((text, back)))
    ov.on_ask_done({"text": "shorter", "focus_returned": True})
    ov.on_ask_done({"text": "again", "focus_returned": True})  # answered once
    assert got == [("shorter", True)]


def test_ghost_is_a_piece_of_the_scene():
    bus = bus_with_monitors()
    ov, run, *_ = make_overlay(bus)
    ov.ghost.show(" tomorrow", 300, 200, 15, 400)
    run()
    assert last_scene(bus)["ghost"]["text"] == " tomorrow"
    ov.ghost.hide()
    run()
    assert last_scene(bus)["ghost"] is None


# ---------------------------------------------------------------- hotkeys


def test_to_accel():
    assert to_accel("Super+Alt+space") == "<Alt><Super>space"
    assert to_accel("Alt+Return") == "<Alt>Return"
    assert to_accel("Control+Right") == "<Control>Right"
    assert to_accel("Escape") == "Escape"
    with pytest.raises(ValueError):
        to_accel("Hyper+x")


def test_hotkeys_grab_report_and_resync():
    bus = FakeBus({"Grab": lambda p: {"ok": p["name"] != "taken"}, "Ungrab": {"ok": True}})
    posted: list[Any] = []
    keys = ShellHotkeys(bus, posted.append)
    fired = []
    assert keys.grab("panel", "Super+Alt+space", lambda: fired.append("panel"))
    assert not keys.grab("taken", "Super+Alt+t", lambda: None)
    assert keys.grabbed() == {"panel": "Super+Alt+space"}
    keys.on_key("panel")
    keys.on_key("unknown")
    for fn in posted:
        fn()
    assert fired == ["panel"]
    bus.calls.clear()
    keys.resync()
    assert [p["name"] for m, p in bus.calls if m == "Grab"] == ["panel", "taken"]
    keys.ungrab("panel")
    assert ("Ungrab", {"name": "panel"}) in bus.calls and "panel" not in keys.grabbed()


def test_hotkeys_without_the_extension_are_remembered():
    bus = FakeBus(available=False)
    keys = ShellHotkeys(bus, lambda fn: fn())
    assert keys.grab("panel", "Super+Alt+space", lambda: None)
    assert keys.grabbed() == {"panel": "Super+Alt+space"}
    with pytest.raises(ValueError):
        keys.grab("bad", "Nope+x", lambda: None)


# ---------------------------------------------------------------- keys


def focused(shell_has_keyboard: bool = False, focus: Any = WINDOW) -> dict[str, Any]:
    return {"focus": focus, "shell_has_keyboard": shell_has_keyboard}


def test_focus_target_is_the_meta_window_unless_the_shell_has_the_keyboard():
    assert ShellKeys(FakeBus({"FocusedWindow": focused()})).focus_target() == (77, 4242)
    assert ShellKeys(FakeBus({"FocusedWindow": focused(True)})).focus_target() == (0, 0)
    assert ShellKeys(FakeBus({"FocusedWindow": focused(focus=None)})).focus_target() == (0, 0)
    assert ShellKeys(FakeBus(available=False)).focus_target() == (0, 0)


def test_typing_is_pinned_and_aborts():
    bus = FakeBus({"TypeText": {"ok": True}, "KeyState": {"down": False, "caps": False}})
    keys = ShellKeys(bus)
    keys.type_text("hello", 2, 77)
    assert bus.calls[-1] == ("TypeText", {"text": "hello", "window": "77", "delay_ms": 2})
    bus.answers["TypeText"] = {"ok": False, "aborted": True}
    with pytest.raises(KeysAborted):
        keys.type_text("hello", 0, 77)
    with pytest.raises(ValueError):
        keys.type_text("naïve", 0, 77)
    assert keys.can_type("plain ASCII!") and not keys.can_type("naïve")
    bus.answers["KeyState"] = {"down": False, "caps": True}
    assert not keys.can_type("abc")
    assert typeable("~") and not typeable("\n")


def test_chord_and_waiting_for_released_modifiers():
    states = iter([{"down": True}, {"down": True}, {"down": False}])
    bus = FakeBus({"Chord": {"ok": True}, "KeyState": lambda _p: next(states)})
    t = [0.0]
    keys = ShellKeys(bus, sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
    keys.chord("ctrl", "v")
    assert ("Chord", {"keys": ["ctrl", "v"]}) in bus.calls
    assert keys.wait_modifiers_released(1000)
    bus.answers["KeyState"] = {"down": True}
    assert not keys.wait_modifiers_released(50)
    bus.answers["Chord"] = {"ok": False, "aborted": True}
    with pytest.raises(KeysAborted):
        keys.chord("ctrl", "z")
    assert ShellKeys(FakeBus(available=False)).any_key_down()  # unknown counts as down


# ---------------------------------------------------------------- clipboard


def test_clipboard_saves_text_only_and_restores_it():
    bus = FakeBus({"ClipboardSave": {"restorable": True, "text": "mine"},
                   "ClipboardSet": {"ok": True}, "ClipboardRestore": {"ok": True}})
    clip = ShellClipboard(bus)
    snap = clip.save()
    assert snap == ClipSnapshot("mine")
    clip.set_secret_text("replacement", 77)
    clip.restore(snap)
    assert bus.calls[-1][0] == "ClipboardRestore" and bus.calls[-1][1]["text"] == "mine"
    bus.answers["ClipboardSave"] = {"restorable": False}
    assert clip.save() is None
    bus.answers["ClipboardSave"] = {"restorable": True, "text": None}
    assert clip.save() == ClipSnapshot(None)


# ---------------------------------------------------------------- selection


def test_selection_reads_primary_and_names_the_focused_window():
    bus = FakeBus({"ReadPrimary": {"text": "selected words"}, "FocusedWindow": focused(),
                   "Pointer": [10, 20]})
    sel = ShellSelection(bus)
    assert sel.primary_text(100) == "selected words"
    assert sel.primary_text(5) is None
    owner = sel.owner()
    assert owner is not None and owner.xid == 77 and owner.wm_class == "org.gnome.TextEditor"
    assert owner.rect == Rect(100, 100, 800, 600)
    assert sel.pointer() == (10, 20)
    bus.answers["FocusedWindow"] = focused(True)
    assert sel.active() is None


# ---------------------------------------------------------------- capture windows


def test_shell_screen_reports_focus_changes_and_geometry():
    bus = FakeBus({"FocusedWindow": {"reason": "query", "focus": WINDOW}})
    screen = ShellScreen(bus)
    win = screen.get_active_window()
    assert win is not None and win.get_pid() == 4242 and win.origin() == (90, 80)
    assert win.get_class_group_name() == "org.gnome.TextEditor"
    assert win.get_geometry() == (100, 100, 800, 600)
    changes, geometry = [], []
    screen.connect("active-window-changed", lambda s, prev: changes.append(prev))
    win.connect("geometry-changed", geometry.append)
    signal = bus.signals["WindowChanged"]
    signal({"reason": "position", "focus": {**WINDOW, "frame": [110, 100, 800, 600],
                                            "buffer": [100, 80, 820, 640]}})
    assert geometry == [win] and win.origin() == (100, 80) and changes == []
    signal({"reason": "focus", "focus": None, "shell_has_keyboard": False})
    assert screen.get_active_window() is None and changes == [win]
    signal({"reason": "focus", "focus": WINDOW, "shell_has_keyboard": True})
    assert screen.get_active_window() is None  # the overview has the keys: no window


# ---------------------------------------------------------------- who may call whom


def test_glanced_accepts_calls_only_from_the_verified_shell():
    from glance.daemon.shell_link import ShellLink

    checked = []

    def is_shell(sender):
        checked.append(sender)
        return sender == ":1.5"

    owner = [":1.5"]
    link = ShellLink({}, None, lambda: owner[0], is_shell=is_shell)
    assert link.allowed(":1.5") and link.allowed(":1.5")
    assert checked == [":1.5"]  # checked once per unique name
    assert not link.allowed(":1.9")  # not the owner of GlanceShell
    owner[0] = ":1.7"  # a new owner that is not gnome-shell (a squatter)
    assert not link.allowed(":1.7")
    owner[0] = None
    assert not link.allowed(":1.5")


def test_gnome_shell_command_line():
    from glance.shellbus import is_gnome_shell

    assert is_gnome_shell(["/usr/bin/gnome-shell"])
    assert is_gnome_shell(["gnome-shell", "--headless", "--wayland"])
    assert not is_gnome_shell(["python3", "gnome-shell"])
    assert not is_gnome_shell([])


def test_extension_command_line_check_with_gjs(tmp_path):
    import shutil
    import subprocess
    from pathlib import Path

    gjs = shutil.which("gjs")
    if gjs is None:
        pytest.skip("gjs is not installed")
    repo = Path(__file__).resolve().parents[1]
    script = tmp_path / "check.js"
    script.write_text(f"""
import {{commandLineIs}} from 'file://{repo}/extension/peers.js';
const cases = [
    ['daemon', ['/home/u/.local/share/glance-src/.venv/bin/python3',
                '/home/u/.local/share/glance-src/.venv/bin/glanced'], true],
    ['daemon', ['/usr/bin/python3', '-m', 'glance.daemon.main'], true],
    ['capture', ['/usr/bin/python3', '-m', 'glance.capture.main'], true],
    ['capture', ['/x/glance-capture'], true],
    ['daemon', ['/usr/bin/python3', '-m', 'glance.capture.main'], false],
    ['daemon', ['/usr/bin/python3', 'evil.py', 'glanced-not'], false],
    ['daemon', ['gdbus', 'call'], false],
    ['daemon', [], false],
];
let bad = cases.filter(([r, a, want]) => commandLineIs(r, a) !== want);
print(bad.length === 0 ? 'OK' : JSON.stringify(bad));
""")
    done = subprocess.run([gjs, "-m", str(script)], capture_output=True, text=True, timeout=30,
                          check=False)
    assert done.stdout.strip() == "OK", done.stdout + done.stderr
