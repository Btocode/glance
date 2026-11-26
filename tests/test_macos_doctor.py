"""`glance doctor` on macOS, with a fake DoctorEnv (runs on Linux; no PyObjC needed)."""
from __future__ import annotations

import re
from pathlib import Path

from glance.doctor.checks import DoctorEnv
from glance.platform.macos import doctor as md
from glance.platform.macos import launchd

REPO = Path(__file__).resolve().parents[1]


def env(tmp_path, status=None, runs=None, **environ):
    calls = []

    def run(argv, timeout=None):
        calls.append(argv)
        for prefix, answer in (runs or {}).items():
            if " ".join(argv).startswith(prefix):
                return answer
        return 1, ""

    def ctl(msg, payload):
        calls.append(["ctl", msg])
        return status if msg == "status" else {"ok": True}

    e = DoctorEnv(home=tmp_path, environ={"GLANCE_MACOS_VERSION": "15.6",
                                          "GLANCE_MACOS_ARCH": "arm64", **environ},
                  run=run, procs=list, ctl=ctl, which=lambda n: None)
    return e, calls


def test_harper_pins_match_fetch_harper():
    text = (REPO / "scripts/fetch-harper.sh").read_text()
    pins = dict(re.findall(r'^(DARWIN_\w+_BINARY_SHA256)="([0-9a-f]{64})"$', text, re.MULTILINE))
    assert pins["DARWIN_ARM64_BINARY_SHA256"] == md.HARPER_SHA256["arm64"]
    assert pins["DARWIN_X86_64_BINARY_SHA256"] == md.HARPER_SHA256["x86_64"]


def test_platform(tmp_path):
    e, _ = env(tmp_path)
    assert md.Platform().run(e).status == "ok"
    e, _ = env(tmp_path, GLANCE_MACOS_VERSION="12.7")
    assert md.Platform().run(e).status == "fail"
    e, _ = env(tmp_path, GLANCE_MACOS_ARCH="ppc")
    assert md.Platform().run(e).status == "fail"


def test_accessibility_names_the_binary_glanced_runs(tmp_path):
    exe = "/Users/x/.local/share/glance/python/cpython-3.12/bin/python3.12"
    e, _ = env(tmp_path, status={"ax_trusted": False, "glanced_exe": exe})
    r = md.Accessibility().run(e)
    assert r.status == "fail" and exe in r.detail and exe in r.hint
    assert "Privacy & Security → Accessibility" in r.hint
    e, _ = env(tmp_path, status={"ax_trusted": True, "capture_ax_trusted": True,
                                 "glanced_exe": exe})
    assert md.Accessibility().run(e).status == "ok"
    e, _ = env(tmp_path, status={"ax_trusted": True, "capture_ax_trusted": False,
                                 "glanced_exe": exe})
    assert md.Accessibility().run(e).status == "fail"


def test_accessibility_without_glanced_names_this_interpreter(tmp_path):
    e, _ = env(tmp_path, status=None)
    r = md.Accessibility().run(e)
    assert r.status == "warn" and md.interpreter() in r.hint


def test_accessibility_fix_asks_glanced_for_the_dialog_and_opens_the_pane(tmp_path):
    e, calls = env(tmp_path, status={"ax_trusted": False})
    assert md.Accessibility().fix(e) is False  # only the user can switch it on
    assert ["ctl", "ax_prompt"] in calls and ["open", md.AX_PANE] in calls


def test_launch_agent(tmp_path):
    e, _ = env(tmp_path)
    assert md.LaunchAgent().run(e).status == "warn"  # not installed
    plist = launchd.agent_path(tmp_path)
    plist.parent.mkdir(parents=True)
    plist.write_bytes(b"x")
    assert "not loaded" in md.LaunchAgent().run(e).detail
    e, calls = env(tmp_path, runs={"launchctl print": (0, "state = running\npid = 7\n")})
    r = md.LaunchAgent().run(e)
    assert r.status == "ok" and "7" in r.detail
    e, calls = env(tmp_path, runs={"launchctl bootstrap": (0, "")})
    assert md.LaunchAgent().fix(e)
    assert calls[-1][:2] == ["launchctl", "bootstrap"]
    e, calls = env(tmp_path, GLANCE_SKIP_LAUNCHCTL="1")
    assert md.LaunchAgent().fix(e) is False and calls == []


def test_daemon_hints_launchctl(tmp_path):
    e, _ = env(tmp_path, status=None)
    r = md.Daemon().run(e)
    assert r.status == "fail" and "launchctl kickstart" in r.hint
    e, _ = env(tmp_path, status={"capture_connected": True, "harper_alive": True})
    assert md.Daemon().run(e).status == "ok"


def test_checks_have_unique_ids_and_no_linux_only_ones():
    ids = [c.id for c in md.CHECKS]
    assert len(ids) == len(set(ids))
    for linux_only in ("session", "a11y", "tray", "desktop_flags", "firefox", "autostart",
                       "clipboard_history"):
        assert linux_only not in ids


def test_hotkeys_reports_the_mac_chords(tmp_path):
    e, _ = env(tmp_path, status={"hotkeys_grabbed": "fix_all,revert,pause,tone1,tone2,tone3,"
                                                    "tone4,complete,selection"})
    r = md.MacHotkeys().run(e)
    assert r.status == "warn" and "check (⌃⌥G)" in r.detail and "panel (⌃⌥O)" in r.detail
    e, _ = env(tmp_path, status={"hotkeys_grabbed": "check,panel,fix_all,revert,pause,tone1,"
                                                    "tone2,tone3,tone4,complete,selection"})
    assert md.MacHotkeys().run(e).status == "ok"
