"""macOS adapters that need a Mac but not the keyboard focus: the run-loop clock, the
pasteboard (saved and put back), a clearly named Keychain test item (deleted again), Carbon
hotkey registration, overlay placement on the real screens, doctor and capture start-up.

    GLANCE_MAC_TEST=1 python -m pytest -m macos tests/macos/test_mac_units.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

pytestmark = pytest.mark.macos


def _spin(seconds: float) -> None:
    from Foundation import NSDate, NSRunLoop

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        NSRunLoop.mainRunLoop().runMode_beforeDate_(
            "kCFRunLoopDefaultMode", NSDate.dateWithTimeIntervalSinceNow_(0.02))


def test_mac_clock_runs_timers_and_posts_from_threads_in_order():
    from glance.platform.macos.runloop import MacClock

    clock = MacClock()
    got: list[str] = []
    clock.call_later(30, lambda: got.append("timer"))
    cancelled = clock.call_later(10, lambda: got.append("cancelled"))
    cancelled.cancel()
    t = threading.Thread(target=lambda: [clock.post(lambda i=i: got.append(f"p{i}"))
                                         for i in range(3)])
    t.start()
    t.join()
    _spin(0.2)
    assert [g for g in got if g.startswith("p")] == ["p0", "p1", "p2"]
    assert "timer" in got and "cancelled" not in got


def test_pasteboard_is_saved_marked_and_restored():
    from AppKit import NSPasteboard

    from glance.platform.macos.clipboard import MARKS, MacClipboard

    pb = NSPasteboard.generalPasteboard()
    clip = MacClipboard()
    before = clip.save()
    assert before is not None
    try:
        clip.set_secret_text("Synthetic replacement text", 0)
        types = set(pb.types() or ())
        assert set(MARKS) <= types
        assert pb.stringForType_("public.utf8-plain-text") == "Synthetic replacement text"
    finally:
        clip.restore(before)
    after = clip.save()
    assert after is not None and after.payload == before.payload


def test_restore_keeps_a_copy_made_in_between():
    from AppKit import NSPasteboard

    from glance.platform.macos.clipboard import MacClipboard

    pb = NSPasteboard.generalPasteboard()
    clip = MacClipboard()
    original = clip.save()
    try:
        clip.set_secret_text("Synthetic", 0)
        pb.clearContents()
        pb.setString_forType_("the user's own copy", "public.utf8-plain-text")
        clip.restore(original)  # someone else wrote: theirs wins
        assert pb.stringForType_("public.utf8-plain-text") == "the user's own copy"
    finally:
        MacClipboard().restore(original)


def test_keychain_store_round_trip_with_a_test_item():
    from glance.platform.macos.keychain import KeychainKeyStore

    store = KeychainKeyStore(service="io.github.btocode.glance.TEST-delete-me")
    assert store.get("testprovider") is None
    try:
        store.set("testprovider", "sk-test-not-a-real-key")
    except RuntimeError as e:
        if "-25308" in str(e):  # errSecInteractionNotAllowed: nothing was written
            pytest.skip("the login keychain is locked (screen locked / no GUI session)")
        raise
    try:
        assert store.get("testprovider") == "sk-test-not-a-real-key"
        store.set("testprovider", "sk-test-second")
        assert store.get("testprovider") == "sk-test-second"
    finally:
        store.delete("testprovider")
    assert store.get("testprovider") is None


def test_carbon_hotkeys_register_and_release():
    from AppKit import NSApplication

    from glance.platform.macos.carbon_hotkeys import CarbonHotkeys

    NSApplication.sharedApplication()
    hk = CarbonHotkeys(lambda fn: fn())
    try:
        assert hk.grab("t1", "Ctrl+Alt+Shift+F12", lambda: None)
        assert hk.grabbed() == {"t1": "Ctrl+Alt+Shift+F12"}
        assert not hk.grab("t2", "Ctrl+Alt+Shift+F12", lambda: None)  # ours already
        hk.ungrab("t1")
        assert hk.grab("t2", "Ctrl+Alt+Shift+F12", lambda: None)
        with pytest.raises(ValueError):
            hk.grab("t3", "Ctrl+Alt+nosuchkey", lambda: None)
    finally:
        hk.close()
    assert hk.grabbed() == {}


def test_overlay_places_the_icon_where_placement_says_on_the_real_screen():
    from AppKit import NSApplication

    from glance.platform.macos.overlay import ICON, ICON_X, ICON_Y, MacOverlay
    from glance.platform.macos.runloop import MacClock
    from glance.protocol import Rect
    from glance.ui.placement import Anchor, place_status
    from glance.ui.status_model import StatusState, StatusView

    NSApplication.sharedApplication().setActivationPolicy_(1)
    ov = MacOverlay(lambda: None, lambda a, k: None, MacClock())
    mon = ov.monitors()[0]
    anchor = Anchor(caret=Rect(mon.x + 300, mon.y + 200, 1, 17),
                    field=Rect(mon.x + 100, mon.y + 180, 500, 100), pid=1)
    try:
        ov.show_status(StatusView(StatusState.READY, count=1), anchor)
        x, y, w, h = ov.status_geometry()
        px, py = place_status(anchor, ICON, ov.monitors())
        assert (x, y) == (px - ICON_X, py - ICON_Y) and (w, h) == (30, 28)
        moved = anchor.moved(40, 25)
        ov.show_status(StatusView(StatusState.READY, count=1), moved)
        x2, y2, _, _ = ov.status_geometry()
        px2, py2 = place_status(moved, ICON, ov.monitors())
        assert (x2, y2) == (px2 - ICON_X, py2 - ICON_Y)
        assert abs((x2 - x) - 40) <= 1 and abs((y2 - y) - 25) <= 1
    finally:
        ov.hide_status()
    assert ov.status_geometry() is None


def test_doctor_runs_on_macos():
    done = subprocess.run([sys.executable, "-m", "glance.cli", "doctor", "--json"],
                          capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode in (0, 1), done.stderr
    import json

    ids = {r["id"] for r in json.loads(done.stdout)}
    assert {"platform", "accessibility", "python", "harper", "launchd"} <= ids


def test_capture_print_mode_starts_and_stops(tmp_path):
    env = {**os.environ, "GLANCE_STRICT_LOG": "1"}
    p = subprocess.Popen([sys.executable, "-m", "glance.capture.main", "--print"], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(2.0)
    p.terminate()
    _out, err = p.communicate(timeout=10)
    assert p.returncode == 0, err
    assert "capture_started mode=print" in err
