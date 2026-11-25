"""The daemon's glue that needs no GTK: capture link, control messages, harper restarts."""
import os
import socket

import pytest

from glance.config import Config, ConfigError
from glance.daemon.control import handle_control
from glance.daemon.controller import Controller
from glance.daemon.links import CaptureLink, PanelEscape
from glance.daemon.main import ConfigReporter, SupervisedHarper, daemon_answers
from glance.daemon.supervisor import ChildSupervisor
from glance.grammar.filters import IssueFilter
from glance.grammar.store import IgnoredRules, PersonalDictionary
from glance.ipc import JsonLineServer
from glance.protocol import (
    APPLY,
    APPLY_RESULT,
    FOCUS_LOST,
    HELLO,
    REVERT,
    SNAPSHOT,
    ApplyRequest,
    ApplyResult,
    Edit,
    FieldSnapshot,
    Rect,
)
from glance.ui.chip_model import ChipState, ChipView
from glance.ui.panel_model import PanelView
from tests.fakes import (
    FakeCapture,
    FakeClock,
    FakeHarper,
    FakeNotifier,
    FakeOverlay,
    FakeTray,
)
from tests.test_hotkeys import FakeHotkeys


def snap(text="Teh quick brown fox jumps", gated=None):
    return FieldSnapshot(
        app="gedit", wm_class="gedit", title="notes", url=None, role="text", text=text,
        caret=len(text), caret_rect=Rect(100, 200, 1, 18), prev_char_rect=None,
        field_rect=Rect(90, 190, 400, 300), window_rect=Rect(50, 50, 800, 600),
        field_id="f1", capture_confidence=1.0, seq=1, pid=100, gated=gated)


# ---------------------------------------------------------------- capture link


class FakeConn:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.closed = False
        self.broken = False

    def send(self, msg_type, payload) -> None:
        if self.broken:
            raise ConnectionError("peer gone")
        self.sent.append((msg_type, payload))

    def close(self) -> None:
        self.closed = True


class Recorder:
    """Stands in for the controller: records the handler calls."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def handle_snapshot(self, s):
        self.calls.append(("snapshot", s))

    def handle_focus_lost(self, reason):
        self.calls.append(("focus_lost", reason))

    def handle_apply_result(self, r):
        self.calls.append(("apply_result", r))

    def handle_capture_connected(self):
        self.calls.append(("connected",))

    def handle_capture_disconnected(self):
        self.calls.append(("disconnected",))


@pytest.fixture
def link():
    posted = []
    link = CaptureLink(posted.append)
    link.attach(Recorder())

    def run():
        while posted:
            posted.pop(0)()

    link.run = run
    link.posted = posted
    return link


def test_capture_messages_reach_the_controller_on_the_main_loop(link):
    conn = FakeConn()
    link.on_message(conn, HELLO, {"role": "capture", "pid": 7})
    link.on_message(conn, SNAPSHOT, snap().to_dict())
    link.on_message(conn, FOCUS_LOST, {"reason": "blur"})
    link.on_message(conn, APPLY_RESULT, {"apply_id": "a1", "status": "verified", "detail": ""})
    assert link.controller.calls == [] and not link.connected  # nothing on the reader thread
    link.run()
    assert link.connected
    assert link.controller.calls == [
        ("connected",), ("snapshot", snap()), ("focus_lost", "blur"),
        ("apply_result", ApplyResult("a1", "verified", ""))]


def test_capture_malformed_messages_are_dropped(link):
    conn = FakeConn()
    link.on_message(conn, HELLO, {})
    link.on_message(conn, SNAPSHOT, {"text": "no other key"})
    link.on_message(conn, SNAPSHOT, {**snap().to_dict(), "caret": "x"})
    link.on_message(conn, APPLY_RESULT, {"status": "verified"})
    link.on_message(conn, FOCUS_LOST, {})
    link.on_message(conn, "bogus", {})
    link.run()
    assert link.controller.calls == [("connected",), ("focus_lost", "")]
    assert link.connected and not conn.closed


def test_capture_sends_apply_and_revert(link):
    conn = FakeConn()
    link.on_message(conn, HELLO, {})
    link.run()
    req = ApplyRequest("a1", "f1", "sha", Edit(0, 3, "The"), "auto")
    link.send_apply(req)
    link.send_revert()
    assert conn.sent == [(APPLY, req.to_dict()), (REVERT, {})]


def test_capture_send_without_a_peer_does_not_raise(link):
    link.send_apply(ApplyRequest("a1", "f1", "sha", Edit(0, 3, "The"), "auto"))
    link.send_revert()
    link.send_paste("x")
    link.send_reset()
    conn = FakeConn()
    link.on_message(conn, HELLO, {})
    link.run()
    conn.broken = True
    link.send_revert()  # the reader thread reports the disconnect


def test_capture_disconnect_is_reported_once_and_only_for_the_live_peer(link):
    old, new = FakeConn(), FakeConn()
    link.on_message(old, HELLO, {})
    link.run()
    link.on_message(new, HELLO, {})  # a restarted capture connects before the old one is gone
    link.run()
    assert old.closed and link.connected
    link.controller.calls.clear()
    link.on_disconnect(old)
    link.run()
    assert link.connected and link.controller.calls == []
    link.on_disconnect(new)
    link.run()
    assert not link.connected and link.controller.calls == [("disconnected",)]
    link.send_revert()
    assert new.sent == []


def test_capture_message_from_a_dropped_peer_is_ignored(link):
    old, new = FakeConn(), FakeConn()
    link.on_message(old, HELLO, {})
    link.on_message(new, HELLO, {})
    link.on_message(old, SNAPSHOT, snap().to_dict())
    link.run()
    assert link.controller.calls == [("connected",), ("connected",)]


def test_a_reconnected_capture_counts_before_its_first_snapshot(tmp_path):
    # No field is focused when capture comes back, so no snapshot follows the HELLO.
    rig = Rig(tmp_path)
    link = CaptureLink(lambda fn: fn())
    c = Controller(
        rig.clock, lambda: rig.cfg, rig.harper,
        IssueFilter(PersonalDictionary(tmp_path / "d2.txt"),
                    IgnoredRules(tmp_path / "i2.json"), []),
        rig.overlay, link, rig.notifier, rig.tray, lambda work, done: done(work()))
    link.attach(c)
    conn = FakeConn()
    link.on_message(conn, HELLO, {})
    assert c.status()["capture_connected"] is True and rig.tray.state == "active"
    link.on_disconnect(conn)
    assert c.status()["capture_connected"] is False and rig.tray.state == "degraded"
    link.on_message(FakeConn(), HELLO, {})
    assert c.status()["capture_connected"] is True and rig.tray.state == "active"


# ---------------------------------------------------------------- control messages


class Rig:
    def __init__(self, tmp_path):
        self.clock = FakeClock()
        self.cfg = Config()
        self.harper = FakeHarper({"Teh": ("Typo", ("The",))})
        self.overlay = FakeOverlay()
        self.capture = FakeCapture()
        self.notifier = FakeNotifier()
        self.tray = FakeTray()
        self.c = Controller(
            self.clock, lambda: self.cfg, self.harper,
            IssueFilter(PersonalDictionary(tmp_path / "d.txt"),
                        IgnoredRules(tmp_path / "i.json"), []),
            self.overlay, self.capture, self.notifier, self.tray,
            lambda work, done: done(work()))


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_control_ping():
    assert handle_control(None, "ping", {}) == {"ok": True, "pid": os.getpid()}


def test_control_status_has_no_text(rig):
    rig.c.handle_snapshot(snap())
    rig.clock.advance(400)
    reply = handle_control(rig.c, "status", {})
    assert reply["ok"] is True and reply["local_issues"] == 1 and reply["paused"] is False
    assert reply["field"] == {"wm_class": "gedit", "gated": None, "len": 25}
    assert "quick" not in repr(reply)


def test_control_pause_and_resume(rig):
    assert handle_control(rig.c, "pause", {"seconds": 60}) == {"ok": True, "paused": True}
    st = rig.c.status()
    assert (st["paused"], st["pause_remaining_s"]) == (True, 60)
    assert handle_control(rig.c, "resume", {}) == {"ok": True, "paused": False}
    assert rig.c.status()["paused"] is False
    assert handle_control(rig.c, "resume", {}) == {"ok": True, "paused": False}


def test_control_pause_without_seconds_is_indefinite(rig):
    for payload in ({"seconds": None}, {}):
        assert handle_control(rig.c, "pause", payload)["paused"] is True
        assert rig.c.status()["pause_remaining_s"] is None
        rig.clock.advance(24 * 3600 * 1000)
        assert rig.c.status()["paused"] is True
        rig.c.resume()


def test_control_pause_again_replaces_the_duration(rig):
    handle_control(rig.c, "pause", {"seconds": 60})
    handle_control(rig.c, "pause", {"seconds": 600})
    rig.clock.advance(61000)
    st = rig.c.status()
    assert (st["paused"], st["pause_remaining_s"]) == (True, 539)


@pytest.mark.parametrize("seconds", [0, -5, "60", 1.5, True, [60]])
def test_control_pause_rejects_bad_seconds(rig, seconds):
    reply = handle_control(rig.c, "pause", {"seconds": seconds})
    assert reply == {"ok": False, "error": "bad seconds"}
    assert rig.c.status()["paused"] is False


def test_control_unknown_message(rig):
    assert handle_control(rig.c, "explode", {}) == {"ok": False, "error": "unknown request"}


# ---------------------------------------------------------------- single instance


def test_daemon_answers_only_when_a_daemon_replies_to_ping(tmp_path):
    path = tmp_path / "glance" / "ctl.sock"
    assert daemon_answers(path) is False  # no socket at all
    server = JsonLineServer(
        path, lambda conn, t, p: conn.send(t, {"id": p["id"], **handle_control(None, t, p)}))
    server.start()
    try:
        assert daemon_answers(path) is True
    finally:
        server.stop()
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()  # the file stays, nobody listens: a crashed daemon
    assert path.exists() and daemon_answers(path) is False


# ---------------------------------------------------------------- escape while the panel is open


def test_escape_is_grabbed_only_while_the_panel_is_shown():
    overlay, keys, pressed = FakeOverlay(), FakeHotkeys(), []
    esc = PanelEscape(overlay, keys, lambda: pressed.append(1))
    view = PanelView(banner="b")
    assert keys.grabbed() == {} and not esc.panel_visible
    esc.show_panel(view, 10, 20)
    esc.show_panel(view, 11, 21)  # every render calls it again
    assert keys.grabbed() == {"escape": "Escape"} and esc.panel_visible
    assert keys.log.count(("grab", "escape")) == 1
    assert (overlay.panel, overlay.panel_xy) == (view, (11, 21))
    keys.grabs["escape"][1]()
    assert pressed == [1]
    esc.hide_panel()
    esc.hide_panel()
    assert keys.grabbed() == {} and not esc.panel_visible and overlay.panel is None
    assert keys.log.count(("ungrab", "escape")) == 1


def test_escape_grab_failure_still_shows_the_panel():
    overlay, keys = FakeOverlay(), FakeHotkeys(taken={"Escape"})
    esc = PanelEscape(overlay, keys, lambda: None)
    esc.show_panel(PanelView(), 1, 2)
    assert overlay.panel == PanelView() and esc.panel_visible
    esc.hide_panel()
    assert overlay.panel is None


def test_panel_escape_passes_the_rest_through():
    overlay = FakeOverlay([Rect(0, 0, 800, 600)])
    esc = PanelEscape(overlay, FakeHotkeys(), lambda: None)
    chip = ChipView(ChipState.FIXES, "2", None)
    esc.show_chip(chip, 5, 6)
    assert (overlay.chip, overlay.chip_xy) == (chip, (5, 6))
    esc.hide_chip()
    assert overlay.chip is None
    assert esc.monitors() == [Rect(0, 0, 800, 600)]
    esc.copy_to_clipboard("x")
    assert overlay.clipboard == ["x"]


# ---------------------------------------------------------------- harper restarts


class FakeHarperClient:
    def __init__(self) -> None:
        self.alive = False
        self.start_error: Exception | None = None
        self.half_started = False  # what a start() that timed out leaves behind
        self.log: list[str] = []
        self.checked: list[str] = []

    def start(self) -> None:
        self.log.append("start")
        if self.start_error is not None:
            self.alive = self.half_started
            raise self.start_error
        self.alive = True

    def stop(self) -> None:
        self.log.append("stop")
        self.alive = False

    def check(self, text):
        self.checked.append(text)
        return ["issue"]


def supervised(client, deferred=None):
    changes = []
    run = (lambda fn: fn()) if deferred is None else deferred.append
    return SupervisedHarper(client, lambda: changes.append(1), run_thread=run), changes


def test_harper_is_not_alive_until_started():
    client = FakeHarperClient()
    harper, _ = supervised(client)
    assert harper.alive is False and harper.check("x") == [] and client.checked == []


def test_harper_spawn_starts_the_client_off_the_main_loop():
    client, pending = FakeHarperClient(), []
    harper, changes = supervised(client, pending)
    child = harper.spawn()
    assert client.log == [] and harper.alive is False
    assert child.poll() is None  # still starting: not a death
    pending.pop()()
    assert client.log == ["stop", "start"] and harper.alive is True and changes == [1]
    assert child.poll() is None
    assert harper.check("text") == ["issue"]


def test_harper_start_timeout_counts_as_a_death_and_leaves_no_child():
    client = FakeHarperClient()
    client.start_error, client.half_started = TimeoutError("initialize"), True
    harper, changes = supervised(client)
    child = harper.spawn()
    assert child.poll() == 1
    assert harper.alive is False and client.alive is False  # the half-started child is gone
    assert client.log == ["stop", "start", "stop"] and changes == []
    assert harper.check("x") == []


def test_harper_missing_binary_counts_as_a_death():
    client = FakeHarperClient()
    client.start_error = FileNotFoundError("harper-ls")
    harper, _ = supervised(client)
    assert harper.spawn().poll() == 1 and harper.alive is False


def test_harper_death_is_seen_and_reported():
    client = FakeHarperClient()
    harper, changes = supervised(client)
    child = harper.spawn()
    client.alive = False  # killed
    assert harper.alive is False and harper.check("x") == [] and client.checked == []
    assert child.poll() == 1 and changes == [1, 1]


def test_supervisor_restarts_a_dead_harper_with_backoff():
    clock, client, states = FakeClock(), FakeHarperClient(), []
    harper, _ = supervised(client)
    sup = ChildSupervisor("harper", harper.spawn, clock, states.append)
    sup.start()
    assert harper.alive
    client.alive = False
    sup.poll()
    assert states == ["up", "restarting"] and not harper.alive  # degraded meanwhile
    clock.advance(200)
    assert harper.alive and states[-1] == "up"
    sup.stop()
    assert not harper.alive and client.log[-1] == "stop"


def test_supervisor_retries_a_harper_whose_start_times_out():
    clock, client, states = FakeClock(), FakeHarperClient(), []
    client.start_error = TimeoutError("initialize")
    harper, _ = supervised(client)
    sup = ChildSupervisor("harper", harper.spawn, clock, states.append)
    sup.start()
    sup.poll()
    assert not harper.alive and states[-1] == "restarting"
    client.start_error = None
    clock.advance(200)
    sup.poll()
    assert harper.alive and states[-1] == "up"


# ---------------------------------------------------------------- config problems


def test_invalid_config_is_reported_once_until_fixed():
    notifier = FakeNotifier()
    reporter = ConfigReporter(notifier)
    assert reporter.status() == {"config_ok": True}
    reporter.invalid(ConfigError("general.chip_idle_ms", "expected an integer"), startup=True)
    assert len(notifier.calls) == 1
    summary, body = notifier.calls[0]
    assert "config" in summary.lower()
    assert "general.chip_idle_ms: expected an integer" in body and "default" in body
    assert reporter.status() == {"config_ok": False}
    reporter.valid()
    assert reporter.status() == {"config_ok": True} and len(notifier.calls) == 1


def test_invalid_config_on_reload_says_the_last_good_one_stays():
    notifier = FakeNotifier()
    reporter = ConfigReporter(notifier)
    reporter.invalid(ConfigError("ui", "expected a table"), startup=False)
    assert "previous settings" in notifier.calls[0][1]
