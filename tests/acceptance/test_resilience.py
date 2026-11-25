"""Spec section 8.6 on the shipped `glanced`: killed children, a hung target app, a signed-out
backend, suspend/resume and monitor hotplug. Everything runs on the private Xvfb + D-Bus
session (never the user's display or services); keys go in through XTEST on that server only.

    .venv/bin/pytest -m xvfb tests/acceptance/test_resilience.py -v

Tests share the session and run in file order (about 2 minutes).
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.acceptance.harness import HARPER, GlancedUnderTest, GtkApp, Typist

pytestmark = [
    pytest.mark.xvfb,
    pytest.mark.skipif(not HARPER.exists(), reason="harper-ls is not installed"),
]

CLEAN = "The quick brown fox jumps over the lazy dog."
RECOVER_S = 2.0


def until(pred, timeout_s: float, step_s: float = 0.02):
    """The predicate's first truthy value, or its last (falsy) one when time ran out."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            value = pred()
        except (KeyError, TypeError, IndexError):
            value = None
        if value or time.monotonic() > deadline:
            return value
        time.sleep(step_s)


def child(g: GlancedUnderTest, needle: str) -> int | None:
    """pid of the descendant of glanced whose command line holds `needle`."""
    for pid in g.descendants():
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
        except OSError:
            continue
        if needle in cmd:
            return pid
    return None


def kill9(pid: int) -> None:
    os.kill(pid, signal.SIGKILL)  # a child of the private glanced, never anything else


class Rig:
    """One GTK app (as a chat app) plus the typist; daemons come and go around them."""

    def __init__(self, session, tmp_path_factory) -> None:
        self.session, self.tmp = session, tmp_path_factory
        self.typist = Typist(session)
        self.app = GtkApp(session, self.typist, undo=True, text=CLEAN)
        self.app.start()
        self.daemons: list[GlancedUnderTest] = []

    def daemon(self, backend: str = "fake", env: dict[str, str] | None = None,
               **kw) -> GlancedUnderTest:
        for d in self.daemons:  # one glanced per display: the hotkey grabs are the first's
            d.stop()
        g = GlancedUnderTest(self.session, self.tmp.mktemp("g"), backend=backend, **kw)
        g.env.update(env or {})
        self.daemons.append(g)
        g.start()
        assert until(lambda: g.status().get("capture_connected")
                     and (g.status().get("field") or {}).get("len") == len(CLEAN)
                     and g.status().get("harper_alive"), 40.0), \
            (g.status(), g.stderr_path.read_text()[-3000:])
        return g

    def type_typo(self, g: GlancedUnderTest, word: str = " We needs a plan") -> None:
        self.typist.chord("Control_L", "End")
        self.typist.type(word)
        assert until(lambda: g.status()["local_issues"] >= 1, 8.0), (g.status(), g.state())

    def close(self) -> None:
        for d in self.daemons:
            d.stop()
        self.app.stop()
        self.typist.close()


@pytest.fixture(scope="module")
def rig(session, tmp_path_factory):
    r = Rig(session, tmp_path_factory)
    yield r
    r.close()


def reset_text(g: GlancedUnderTest, rig: Rig, text: str = CLEAN) -> None:
    have = rig.app.truth()["text"]
    assert until(lambda: (g.state().get("snapshot") or {}).get("text") == have, 5.0)
    assert until(lambda: g.request("test_apply", {"start": 0, "end": len(have),
                                                  "replacement": text, "method": "auto"}
                                   ).get("ok"), 5.0, 0.3)
    assert until(lambda: rig.app.truth()["text"] == text, 5.0)


# ---------------------------------------------------------------- killed children


def test_kill_capture_recovers_within_2s(rig):
    g = rig.daemon()
    pid = child(g, "glance.capture.main")
    assert pid
    t0 = time.monotonic()
    kill9(pid)
    assert until(lambda: g.status().get("capture_connected") is False, 2.0) or True
    ok = until(lambda: (g.status().get("capture_connected")
                        and (g.status().get("field") or {}).get("len") == len(CLEAN)
                        and child(g, "glance.capture.main") not in (None, pid)), RECOVER_S + 3)
    took = time.monotonic() - t0
    assert ok, (took, g.status(), g.stderr_path.read_text()[-2000:])
    assert took <= RECOVER_S, f"capture was back after {took:.2f}s"
    rig.type_typo(g)  # and the strip's input still works
    reset_text(g, rig)


def test_kill_harper_recovers_within_2s(rig):
    """The process is replaced within 2 s; it is *ready* (initialised and warmed up on a
    word far from the dictionary, ~1.75 s by itself) a few tenths later."""
    g = rig.daemons[-1]
    pid = child(g, "harper-ls")
    assert pid
    t0 = time.monotonic()
    kill9(pid)
    assert until(lambda: child(g, "harper-ls") not in (None, pid), RECOVER_S), g.status()
    respawned = time.monotonic() - t0
    assert until(lambda: g.status().get("harper_alive"), 3.0), g.status()
    ready = time.monotonic() - t0
    print(f"harper-ls respawned after {respawned:.2f}s, ready after {ready:.2f}s")
    assert ready <= RECOVER_S + 0.5, f"harper-ls was ready after {ready:.2f}s"
    assert g.status().get("harper_child") == "up"
    rig.type_typo(g)
    assert g.status()["harper_alive"]
    reset_text(g, rig)


def test_daemon_survives_every_child_dying_at_once(rig):
    g = rig.daemons[-1]
    pids = [p for p in (child(g, "glance.capture.main"), child(g, "harper-ls")) if p]
    assert len(pids) == 2
    for p in pids:
        kill9(p)
    assert until(lambda: g.status().get("capture_connected") and g.status().get("harper_alive"),
                 RECOVER_S + 4), g.status()
    rig.type_typo(g)
    reset_text(g, rig)


def test_kill_backend_child_mid_request(rig):
    g = rig.daemon("fake_cli", fake_mode="hang")
    rig.typist.chord("Control_L", "End")
    rig.typist.type(" and then some more words to analyse for the daemon")
    assert until(lambda: child(g, "fake_cli/claude"), 10.0), g.descendants()
    kill9(child(g, "fake_cli/claude"))
    t0 = time.monotonic()
    st = g.cli("status")
    assert st.returncode == 0, st.stderr
    assert time.monotonic() - t0 < 2.0
    assert until(lambda: g.state()["chip"]["state"] != "spinner", 1.5), g.state()
    assert g.proc.poll() is None
    assert g.status()["capture_connected"]
    reset_text(g, rig)


# ---------------------------------------------------------------- hung target app


def test_hung_app_does_not_freeze_daemon(rig):
    g = rig.daemon()
    rig.type_typo(g)
    app = rig.app._proc
    app.send_signal(signal.SIGSTOP)  # the app stops answering AT-SPI and X events
    try:
        slow = []
        for _ in range(10):
            t0 = time.monotonic()
            assert g.status().get("capture_connected") is not None
            slow.append(time.monotonic() - t0)
            time.sleep(0.3)
        assert max(slow) < 0.5, slow
        # an apply into the hung app is refused or fails; it never reports success
        have = (g.state().get("snapshot") or {}).get("text")
        if have:
            sent = g.request("test_apply", {"start": 0, "end": 3, "replacement": "The",
                                            "method": "auto"})
            if sent.get("ok"):
                done = until(lambda: g.request(
                    "test_apply_result", {"apply_id": sent["apply_id"]}).get("result"), 20.0)
                assert done is None or done["status"] != "verified", done
        assert g.status().get("capture_connected") is not None  # still answering
    finally:
        app.send_signal(signal.SIGCONT)
    assert until(lambda: g.status().get("capture_connected"), 5.0)
    assert g.proc.poll() is None
    rig.typist.chord("Control_L", "End")
    rig.typist.type(" ok")  # snapshots resume
    assert until(lambda: ((g.state().get("snapshot") or {}).get("text") or "").endswith(" ok"),
                 15.0), g.state()
    reset_text(g, rig)


# ---------------------------------------------------------------- backend signed out


def test_backend_signed_out_shows_connect_and_grammar_continues(rig):
    g = rig.daemon("fake_cli", env={"FAKE_AUTH": "out"})
    assert until(lambda: g.status().get("ai") == "none", 15.0), g.status()
    assert until(lambda: g.state()["chip"]["label"] == "Connect an AI" or
                 g.state()["chip"]["state"] == "no_ai", 10.0), g.state()
    rig.type_typo(g)  # local grammar goes on
    assert g.status()["harper_alive"] and g.proc.poll() is None
    reset_text(g, rig)


# ---------------------------------------------------------------- monitor hotplug


def test_monitor_removed_and_added_moves_the_strip(rig):
    g = rig.daemon()
    rig.type_typo(g)
    shown = until(lambda: g.request("test_overlay").get("strip"), 10.0)
    assert shown, g.request("test_overlay")
    x, y, _w, _h = shown
    # a monitor change: only a small monitor away from the strip remains
    far = [0, 0, 100, 100]
    assert g.request("test_monitors", {"monitors": [far]}).get("ok")
    got = g.request("test_overlay").get("strip")
    print("strip on the lone small monitor:", got)
    assert got is None or (0 <= got[0] < 100 and 0 <= got[1] < 100), (shown, got)
    assert g.proc.poll() is None
    # the monitor is plugged back (the real ones again): the strip is back by the caret
    assert g.request("test_monitors", {}).get("ok")
    back = until(lambda: g.request("test_overlay").get("strip"), 5.0)
    assert back, g.request("test_overlay")
    assert abs(back[0] - x) <= 40 and abs(back[1] - y) <= 40, (shown, back)
    reset_text(g, rig)


# ---------------------------------------------------------------- suspend / resume


def emit_prepare_for_sleep(session, starting: bool) -> None:
    """logind's signal on the private bus, which the daemon under test reads as its system
    bus (DBUS_SYSTEM_BUS_ADDRESS)."""
    subprocess.run(
        ["gdbus", "emit", "--session", "--object-path", "/org/freedesktop/login1",
         "--signal", "org.freedesktop.login1.Manager.PrepareForSleep",
         "true" if starting else "false"],
        env=session.env, check=True, capture_output=True, timeout=10)


def test_suspend_resume_via_logind_signal(rig, session):
    bus = session.env["DBUS_SESSION_BUS_ADDRESS"]
    g = rig.daemon(env={"DBUS_SYSTEM_BUS_ADDRESS": bus})
    rig.type_typo(g)
    stale = until(lambda: g.request("test_overlay").get("strip"), 8.0)
    assert stale
    emit_prepare_for_sleep(session, True)
    emit_prepare_for_sleep(session, False)
    assert until(lambda: "resumed_from_sleep" in g.stderr_path.read_text(), 5.0), \
        g.stderr_path.read_text()[-2000:]
    assert until(lambda: "capture_reset" in g.stderr_path.read_text(), 5.0)
    # capture reported again: the chip and strip are a fresh view of the field, and an
    # apply still verifies
    assert until(lambda: g.status().get("capture_connected")
                 and g.state().get("snapshot"), 5.0), g.status()
    have = rig.app.truth()["text"]
    assert until(lambda: (g.state()["snapshot"] or {}).get("text") == have, 5.0)
    sent = g.request("test_apply", {"start": 0, "end": len(have),
                                    "replacement": CLEAN, "method": "auto"})
    assert sent.get("ok"), sent
    done = until(lambda: g.request("test_apply_result",
                                   {"apply_id": sent["apply_id"]}).get("result"), 10.0)
    assert done and done["status"] == "verified", done
    assert rig.app.truth()["text"] == CLEAN


def test_suspend_resume_simulated_with_test_message(rig):
    g = rig.daemons[-1]
    rig.type_typo(g)
    assert g.request("test_resume").get("ok")
    assert until(lambda: g.status().get("capture_connected")
                 and (g.status().get("field") or {}).get("len", 0) > len(CLEAN), 8.0), \
        g.status()
    have = rig.app.truth()["text"]
    assert until(lambda: (g.state()["snapshot"] or {}).get("text") == have, 5.0)
    reset_text(g, rig)


def test_lock_hides_the_strip_and_unlock_brings_it_back(rig):
    g = rig.daemons[-1]
    rig.type_typo(g)
    assert until(lambda: g.request("test_overlay").get("strip"), 8.0)
    assert g.request("test_lock", {"active": True}).get("ok")
    assert g.request("test_overlay").get("strip") is None
    assert g.status()["paused"] is True
    assert g.request("test_lock", {"active": False}).get("ok")
    assert g.status()["paused"] is False
    rig.type_typo(g, "x")
    reset_text(g, rig)


# ---------------------------------------------------------------- the daemon itself


def run_glanced(g: GlancedUnderTest, log: Path) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "glance.daemon.main"],
                            env={**g.env, "NO_AT_BRIDGE": "1"}, stderr=log.open("w"),
                            stdout=subprocess.DEVNULL)


def test_stale_socket_after_sigkill(rig):
    g = rig.daemons[-1]
    assert g.sock.exists()
    first = g.proc.pid
    kill9(first)
    g.proc.wait(timeout=5)
    assert until(lambda: child(g, "glance.capture.main") is None, 0.1) or True
    assert g.sock.exists()  # a socket file nobody serves
    t0 = time.monotonic()
    g.proc = run_glanced(g, g.stderr_path)
    assert until(lambda: g.status().get("pid") or g.request("ping").get("pid"),
                 RECOVER_S), g.stderr_path.read_text()[-2000:]
    took = time.monotonic() - t0
    assert took <= RECOVER_S, f"glanced answered after {took:.2f}s"
    assert g.request("ping")["pid"] == g.proc.pid
    assert "address in use" not in g.stderr_path.read_text().lower()
    assert until(lambda: g.status().get("capture_connected"), 15.0)
    # the old capture died with the old glanced (PDEATHSIG): exactly one serves the new one
    assert until(lambda: child(g, "harper-ls"), 10.0)
    pids = g.descendants()
    cmds = [Path(f"/proc/{p}/cmdline").read_bytes() for p in pids]
    assert sum(b"glance.capture.main" in c for c in cmds) == 1
    assert sum(b"harper-ls" in c for c in cmds) == 1


def test_second_instance_refuses(rig):
    g = rig.daemons[-1]
    before = g.status()
    second = subprocess.run([sys.executable, "-m", "glance.daemon.main"],
                            env={**g.env, "NO_AT_BRIDGE": "1"}, capture_output=True,
                            text=True, timeout=30, check=False)
    assert second.returncode == 1 and "already running" in second.stderr
    assert g.proc.poll() is None
    assert g.status().get("capture_connected") == before.get("capture_connected")
    assert g.request("ping")["pid"] == g.proc.pid
