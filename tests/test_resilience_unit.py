"""Suspend/resume, lock, monitor hotplug and degraded-mode behaviour of the controller."""
from glance.backends.base import BackendError
from glance.daemon.session import SessionEvents
from glance.grammar.issues import issue_edit
from glance.protocol import ApplyResult, Edit, Rect
from glance.ui.chip_model import ChipState
from tests.test_ai_analyze import TYPO
from tests.test_ai_analyze import Rig as AiRig
from tests.test_ai_analyze import snap as ai_snap
from tests.test_controller import MIXED, Rig, snap


class Listener:
    def __init__(self):
        self.resets = 0
        self.results = []

    def on_reset(self):
        self.resets += 1

    def on_apply_result(self, r):
        self.results.append(r)


def test_resume_cancels_and_resets(tmp_path):
    rig = Rig(tmp_path)
    listener = Listener()
    rig.c.add_listener(listener)
    rig.settle(text=MIXED)
    gen = rig.c.generation
    apply_id = rig.c.request_apply(issue_edit(rig.c.issues[0]))
    assert apply_id and rig.overlay.chip is not None
    rig.c.handle_resume()
    assert rig.capture.resets == 1 and listener.resets == 1
    assert rig.c.generation > gen
    assert rig.overlay.chip is None and rig.overlay.strip is None and rig.overlay.panel is None
    assert rig.c.current is None
    # the late result of the pre-sleep apply changes nothing the user sees
    rig.c.handle_apply_result(ApplyResult(apply_id, "failed", "late"))
    assert rig.notifier.calls == [] and rig.overlay.chip is None
    # and a new apply is possible: nothing is pending any more
    rig.c.handle_snapshot(snap(text=MIXED))
    rig.clock.advance(400)
    assert rig.c.request_apply(Edit(0, 3, "The"))


def test_lock_hides_and_blocks_processing(tmp_path):
    rig = Rig(tmp_path)
    rig.settle()
    n0 = len(rig.harper.calls)
    rig.c.handle_lock(True)
    assert rig.overlay.chip is None and rig.overlay.strip is None
    rig.c.handle_snapshot(snap(text="Teh quick brwon fox"))
    rig.clock.advance(2000)
    assert len(rig.harper.calls) == n0 and rig.overlay.chip is None
    rig.c.handle_lock(False)
    rig.c.handle_snapshot(snap(text="Teh quick brwon fox again"))
    rig.clock.advance(400)
    assert rig.overlay.chip is not None
    assert len(rig.harper.calls) > n0


def test_lock_preserves_user_pause(tmp_path):
    rig = Rig(tmp_path)
    rig.settle()
    rig.c.toggle_pause()
    rig.c.handle_lock(True)
    rig.c.handle_lock(False)
    assert rig.c.status()["paused"] is True
    rig.c.handle_lock(True)
    rig.c.handle_lock(True)  # a repeated signal changes nothing
    rig.c.handle_lock(False)
    assert rig.c.status()["paused"] is True


def test_lock_without_pause_is_not_left_paused(tmp_path):
    rig = Rig(tmp_path)
    rig.settle()
    rig.c.handle_lock(True)
    assert rig.c.status()["paused"] is True
    rig.c.handle_lock(False)
    assert rig.c.status()["paused"] is False


def test_monitor_unplug_replaces_chip(tmp_path):
    rig = Rig(tmp_path)
    rig.overlay.monitor_list = [Rect(0, 0, 1920, 1080), Rect(1920, 0, 1920, 1080)]
    rig.settle(caret_rect=Rect(2500, 200, 1, 18))
    assert rig.overlay.chip_xy is not None and rig.overlay.chip_xy[0] >= 1920
    rig.overlay.monitor_list = [Rect(0, 0, 1920, 1080)]
    rig.c.handle_monitors_changed()
    assert rig.overlay.chip_xy is None or rig.overlay.chip_xy[0] < 1920
    # plugged in again: the chip comes back where the caret is
    rig.overlay.monitor_list = [Rect(0, 0, 1920, 1080), Rect(1920, 0, 1920, 1080)]
    rig.c.handle_monitors_changed()
    assert rig.overlay.chip_xy is not None and rig.overlay.chip_xy[0] >= 1920


def test_clock_jump_does_not_fire_stale_timers(tmp_path):
    rig = AiRig(tmp_path)
    rig.c.handle_snapshot(ai_snap())
    rig.clock.advance(300)  # asleep before the analyze idle time
    rig.c.handle_resume()
    rig.clock.advance(8 * 3600 * 1000)
    assert rig.backend.calls == []
    assert rig.overlay.chip is None


def test_backend_rate_limit_keeps_local_grammar(tmp_path):
    rig = AiRig(tmp_path, patterns=TYPO)
    rig.backend.script(*[BackendError("rate_limit", "429")] * 3)
    text = "send me teh report now, i need it before the meeting starts."
    for n in range(3):  # the breaker opens after three failures
        rig.analyzed(ai_snap(text=text + " " * n))
    rig.analyzed(ai_snap(text="send me teh report now, i need it before the meeting ends."))
    assert [i.source for i in rig.c.issues] == ["harper"]
    assert rig.overlay.chip.state in (ChipState.FIXES, ChipState.IDLE)
    assert rig.overlay.chip.badge == 1 or rig.overlay.chip.label.endswith("1")
    assert rig.notifier.calls == []
    assert len(rig.backend.calls) <= 3  # the open breaker stopped asking


def test_harper_restart_recovers_issues(tmp_path):
    rig = Rig(tmp_path)
    rig.harper.alive = False
    rig.settle(text=MIXED)
    assert rig.c.issues == () and rig.tray.state == "degraded"
    rig.harper.alive = True
    rig.c.recheck()
    assert len(rig.c.issues) == 3 and rig.tray.state == "active"


def test_session_events_route_signals():
    calls = []
    ev = SessionEvents(lambda: calls.append("sleep"), lambda: calls.append("resume"),
                       lambda on: calls.append(("lock", on)), lambda: calls.append("mon"))
    ev.prepare_for_sleep(True)
    ev.prepare_for_sleep(False)
    ev.active_changed(True)
    ev.active_changed(False)
    ev.monitors_changed()
    assert calls == ["sleep", "resume", ("lock", True), ("lock", False), "mon"]
