"""End to end on a Mac: glanced (fake AI backend) and the test app in the console session.

What a user sees: the status icon for a focused field, the strip on a click, ⌥⏎ writes the
corrected sentence (checked by AX), the overlay follows a window move, the test app keeps the
focus throughout, a password field is never read, select-anything reads a selection, and no
typed text reaches the logs. Keys and clicks go only to the test app, after checking that it
is frontmost; the pointer and the clipboard are put back afterwards.

Run on the Mac (console session, Accessibility granted to the Python running this):
    GLANCE_MAC_TEST=1 python -m pytest -m macos tests/macos -v
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.macos

HERE = Path(__file__).parent
WRONG = "Zebrafinch said this are a important test of the system."
SECRET = "Quokkasecret 4ever"
SELECTED = "Wombatline these is a selection test."


def _wait(fn, timeout=8.0, step=0.1):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(step)
    return last


BUNDLE_ID = "io.github.btocode.glance.testapp"
INFO_PLIST = {"CFBundleIdentifier": BUNDLE_ID, "CFBundleName": "GlanceTestApp",
              "CFBundleExecutable": "run", "CFBundlePackageType": "APPL",
              "CFBundleVersion": "1", "NSHighResolutionCapable": True}


def make_bundle(root: Path) -> Path:
    """A minimal .app whose executable is this Python (a symlink) with the venv's packages on
    PYTHONPATH; `open --args textapp.py` runs the test app in it. Launched with `open`, it goes
    through LaunchServices: since macOS 14 only such an app may take the focus. (A shell-script
    executable is refused by LaunchServices with -10669.)"""
    import plistlib
    import site

    app = root / "GlanceTestApp.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    info = {**INFO_PLIST, "LSEnvironment": {"PYTHONPATH": os.pathsep.join(
        site.getsitepackages())}}
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    (app / "Contents" / "MacOS" / "run").symlink_to(sys.executable)
    return app


class App:
    def __init__(self, bundle: Path, sock: Path):
        import socket

        self.bundle = bundle
        subprocess.run(["open", "-n", "-a", str(bundle), "--args", str(HERE / "textapp.py"),
                        "--sock", str(sock)], check=True, timeout=20)
        assert _wait(sock.exists, 20), "the test app did not start"
        self.conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.conn.connect(str(sock))
        self.rfile = self.conn.makefile("r", encoding="utf-8")
        self.wfile = self.conn.makefile("w", encoding="utf-8")
        self.pid = json.loads(self.rfile.readline())["pid"]

    def __call__(self, cmd, **kw):
        self.wfile.write(json.dumps({"cmd": cmd, **kw}) + "\n")
        self.wfile.flush()
        reply = json.loads(self.rfile.readline())
        assert reply.get("ok"), reply
        return reply

    def bring_forward(self) -> None:
        """`open -a` on the running bundle: LaunchServices activates that instance."""
        subprocess.run(["open", "-a", str(self.bundle)], check=False, timeout=20)

    def stop(self) -> None:
        import contextlib

        with contextlib.suppress(Exception):  # it may be gone already
            self("quit")
        self.conn.close()
        if not _wait(lambda: not _alive(self.pid), 5):
            os.kill(self.pid, 9)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _frontmost_pid() -> int:
    from AppKit import NSWorkspace
    from Foundation import NSDate, NSRunLoop

    # NSWorkspace learns about activations through the run loop: let it run a moment.
    NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.02))

    front = NSWorkspace.sharedWorkspace().frontmostApplication()
    return int(front.processIdentifier()) if front is not None else 0


def _ax_value(pid: int) -> str | None:
    import ApplicationServices as AX

    app = AX.AXUIElementCreateApplication(pid)
    err, el = AX.AXUIElementCopyAttributeValue(app, "AXFocusedUIElement", None)
    if err or el is None:
        return None
    err, v = AX.AXUIElementCopyAttributeValue(el, "AXValue", None)
    return None if err else str(v)


_MOD_KEYS = ((0x40000, 59), (0x80000, 58), (0x20000, 56), (0x100000, 55))  # ⌃ ⌥ ⇧ ⌘


def _key(code: int, flags: int) -> None:
    """Press `code` with the modifiers in `flags` held, the way a keyboard does: each modifier
    goes down, the key goes down and up, the modifiers come up again. (A key event that only
    carries the flags leaves the system believing ⌥ is still held.)"""
    import Quartz as Q

    def post(c: int, down: bool, f: int) -> None:
        ev = Q.CGEventCreateKeyboardEvent(None, c, down)
        Q.CGEventSetFlags(ev, f)
        Q.CGEventPost(Q.kCGHIDEventTap, ev)
        time.sleep(0.02)

    mods = [(bit, c) for bit, c in _MOD_KEYS if flags & bit]
    held = 0
    for bit, c in mods:
        held |= bit
        post(c, True, held)
    post(code, True, held)
    post(code, False, held)
    for bit, c in reversed(mods):
        held &= ~bit
        post(c, False, held)


def _release_modifiers() -> None:
    import Quartz as Q

    for _bit, c in _MOD_KEYS:
        ev = Q.CGEventCreateKeyboardEvent(None, c, False)
        Q.CGEventSetFlags(ev, 0)
        Q.CGEventPost(Q.kCGHIDEventTap, ev)


def _click(x: float, y: float) -> None:
    import Quartz as Q

    back = Q.CGEventGetLocation(Q.CGEventCreate(None))
    for kind in (Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp):
        Q.CGEventPost(Q.kCGHIDEventTap,
                      Q.CGEventCreateMouseEvent(None, kind, (x, y), Q.kCGMouseButtonLeft))
        time.sleep(0.05)
    Q.CGWarpMouseCursorPosition(back)


@pytest.fixture(scope="module")
def world():
    import ApplicationServices as AX

    if not AX.AXIsProcessTrusted():
        pytest.skip(f"Accessibility is not granted to {os.path.realpath(sys.executable)}")
    import Quartz

    d = Quartz.CGSessionCopyCurrentDictionary()
    if d and d.get("CGSSessionScreenIsLocked"):
        pytest.skip("the console session is locked: unlock the Mac to run these")
    from glance.platform.macos.clipboard import MacClipboard

    clip = MacClipboard()
    saved = clip.save()
    root = Path(tempfile.mkdtemp(prefix="r", dir=str(Path.home() / "glance-mactest")))
    env = {**os.environ, "XDG_CONFIG_HOME": str(root / "c"), "XDG_DATA_HOME": str(root / "d"),
           "XDG_STATE_HOME": str(root / "s"), "GLANCE_TEST": "1", "GLANCE_FAKE_BACKEND": "1",
           "GLANCE_STRICT_LOG": "1"}
    env.pop("GLANCE_PAID", None)
    (root / "d" / "glance" / "bin").mkdir(parents=True)
    harper = Path(os.environ.get("GLANCE_HARPER", Path.home() / "glance-mactest/xdg/data/glance/"
                                 "bin/harper-ls"))
    shutil.copy2(harper, root / "d" / "glance" / "bin" / "harper-ls")
    (root / "c" / "glance").mkdir(parents=True)
    (root / "c" / "glance" / "config.toml").write_text("[general]\ncheck_updates = false\n")
    log = open(root / "glanced.log", "wb")  # noqa: SIM115
    from glance.ipc import JsonLineClient

    app = App(make_bundle(root), root / "app.sock")
    daemon = subprocess.Popen([sys.executable, "-m", "glance.daemon.main"], env=env,
                              stdout=log, stderr=subprocess.STDOUT)
    ctl = None
    try:
        sock = root / "s" / "glance" / "run" / "ctl.sock"
        assert _wait(sock.exists, 15), "glanced did not start"
        ctl = JsonLineClient(sock)
        ctl.connect(timeout_s=5)
        yield {"app": app, "ctl": ctl, "root": root, "daemon": daemon, "env": env}
    finally:
        _release_modifiers()
        app.stop()
        if ctl is not None:
            ctl.close()
        daemon.terminate()
        try:
            daemon.wait(10)
        except subprocess.TimeoutExpired:
            daemon.kill()
        log.close()
        if saved is not None:
            clip.restore(saved)
        if os.environ.get("GLANCE_KEEP_RUN") != "1":
            shutil.rmtree(root, ignore_errors=True)


def overlay(w) -> dict:
    return w["ctl"].request("test_overlay", {}, timeout_s=3)


def overlay_when(w, pred, timeout=8.0) -> dict | None:
    """The overlay state once `pred(state)` holds, None if it never does."""
    def probe():
        o = overlay(w)
        return o if pred(o) else None

    return _wait(probe, timeout)


def front(w) -> None:
    app = w["app"]
    if _frontmost_pid() != app.pid:
        app("activate")
        if not _wait(lambda: _frontmost_pid() == app.pid, 2):
            app.bring_forward()
    assert _wait(lambda: _frontmost_pid() == app.pid, 5), "the test app is not frontmost"
    app("activate")  # its window key too


def test_status_icon_appears_for_a_focused_field(world):
    front(world)
    world["app"]("focus", what="view")
    world["app"]("set", what="view", text=WRONG)
    o = overlay_when(world, lambda o: o["status"], 15)
    assert o and o["status"], overlay(world)
    ready = overlay_when(world, lambda o: (o["status_state"] or {}).get("state") == "ready", 15)
    assert ready, overlay(world)
    assert _frontmost_pid() == world["app"].pid


def test_strip_appears_on_icon_click_and_focus_stays(world):
    o = overlay(world)
    x, y, _w, _h = o["status"]
    assert _frontmost_pid() == world["app"].pid
    _click(x + 12, y + 16)
    o = overlay_when(world, lambda o: o["strip"], 5)
    assert o and o["strip"] and o["strip_text"], overlay(world)
    assert o["can_use"]
    assert _frontmost_pid() == world["app"].pid, "clicking the icon changed the focus"
    world["strip_text"] = o["strip_text"]


def test_option_return_applies_the_corrected_sentence(world):
    assert _frontmost_pid() == world["app"].pid
    o = overlay(world)
    assert o["enter_grabbed"], o
    _key(36, 0x80000)  # ⌥⏎
    want = world["strip_text"]
    # Glance's own verdict first, without touching the test app meanwhile: its Python main
    # thread answering our socket polls kept it from answering capture's AX re-read.
    log = world["root"] / "glanced.log"
    done = _wait(lambda: "writeback_apply status=" in log.read_text(errors="replace"), 8)
    assert done, "no apply result"
    assert "writeback_apply status=verified" in log.read_text(errors="replace")
    got = _wait(lambda: world["app"]("get", what="view")["text"] == want and want, 8)
    assert got == want, (world["app"]("get", what="view")["text"], want)
    assert _ax_value(world["app"].pid) == want
    assert _frontmost_pid() == world["app"].pid


def test_overlay_follows_a_window_move(world):
    front(world)
    world["app"]("set", what="view", text=WRONG)
    before = _wait(lambda: overlay(world)["status"], 10)
    assert before
    world["app"]("move", dx=80, dy=-50)  # AppKit: -dy is down on screen
    o = overlay_when(world, lambda o: o["status"] and o["status"] != before, 6)
    assert o, "the status icon did not follow the window"
    moved = o["status"]
    # y follows the caret line exactly; x is the field's right edge, clamped to the screen
    assert abs((moved[1] - before[1]) - 50) <= 3
    assert 0 < moved[0] - before[0] <= 83


def test_password_field_is_never_read(world):
    front(world)
    world["app"]("focus", what="secure")
    world["app"]("set", what="secure", text=SECRET)
    time.sleep(1.5)
    o = overlay(world)
    assert o["strip"] is None
    st = world["ctl"].request("status", {}, timeout_s=3)
    assert SECRET not in json.dumps(st)
    assert o["status_state"] is None or o["status_state"]["state"] in ("muted", "hidden"), o


def test_select_anything_reads_a_selection(world):
    front(world)
    world["app"]("focus", what="view")
    world["app"]("set", what="view", text=SELECTED)
    world["app"]("select", start=0, end=len(SELECTED))
    time.sleep(0.5)
    assert _frontmost_pid() == world["app"].pid
    _key(0, 0x40000 | 0x80000)  # ⌃⌥A
    o = overlay_when(world, lambda o: o["panel"], 8)
    assert o and o["panel"], overlay(world)
    st = world["ctl"].request("status", {}, timeout_s=3)
    assert st.get("selection_open") is True, st
    _key(53, 0)  # Esc closes it
    assert _wait(lambda: overlay(world)["panel"] is None, 4)


def test_nothing_typed_reaches_the_logs(world):
    time.sleep(0.5)
    text = (world["root"] / "glanced.log").read_text(errors="replace")
    for word in ("Zebrafinch", "Quokkasecret", "Wombatline", "important test"):
        assert word not in text
    assert "glanced_started" in text and "capture_started" in text
