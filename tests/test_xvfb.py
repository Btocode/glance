"""The X adapters against a private Xvfb server: real XGrabKey, real GTK windows.

Nothing here touches the display the user is working on: every connection goes to the
Xvfb started by the fixture, and XTEST input is sent to that server only.
"""
from __future__ import annotations

import contextlib
import json
import os
import queue
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

PROBE = Path(__file__).parent / "desktop" / "overlay_probe.py"

pytestmark = pytest.mark.skipif(shutil.which("Xvfb") is None, reason="Xvfb is not installed")


@pytest.fixture(scope="module")
def xvfb():
    """Display name of a fresh Xvfb (1280x800), e.g. `:99`."""
    read_fd, write_fd = os.pipe()
    proc = subprocess.Popen(
        ["Xvfb", "-displayfd", str(write_fd), "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
        pass_fds=[write_fd], stderr=subprocess.DEVNULL)
    os.close(write_fd)
    try:
        with os.fdopen(read_fd) as f:
            number = f.readline().strip()
        assert number, "Xvfb did not start"
        yield f":{number}"
    finally:
        proc.terminate()
        proc.wait(timeout=5)


class Keyboard:
    """Presses keys on the Xvfb server through XTEST."""

    def __init__(self, name: str) -> None:
        from Xlib import display

        self.d = display.Display(name)

    def chord(self, *keys: str) -> None:
        from Xlib import XK, X
        from Xlib.ext import xtest

        codes = [self.d.keysym_to_keycode(XK.string_to_keysym(k)) for k in keys]
        assert all(codes), keys
        for code in codes:
            xtest.fake_input(self.d, X.KeyPress, code)
        for code in reversed(codes):
            xtest.fake_input(self.d, X.KeyRelease, code)
        self.d.sync()

    def close(self) -> None:
        self.d.close()


@pytest.fixture
def keys(xvfb):
    from glance.ui.hotkeys_xlib import XlibHotkeys

    fired: queue.Queue = queue.Queue()
    made = []

    def make() -> XlibHotkeys:
        made.append(XlibHotkeys(lambda cb: cb(), xvfb))
        return made[-1]

    keyboard = Keyboard(xvfb)

    def presses(*chord: str, wait: float = 0.5) -> list[str]:
        keyboard.chord(*chord)
        got = []
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                got.append(fired.get(timeout=0.05))
            except queue.Empty:
                if got:
                    break
        return got

    yield make, fired, presses
    for h in made:
        h.close()
    keyboard.close()


def test_grabbed_chord_fires_once_per_press(keys):
    make, fired, presses = keys
    h = make()
    assert h.grab("fix_all", "Super+Alt+f", lambda: fired.put("fix_all")) is True
    assert h.grab("panel", "Super+Alt+space", lambda: fired.put("panel")) is True
    assert h.grabbed() == {"fix_all": "Super+Alt+f", "panel": "Super+Alt+space"}
    assert presses("Super_L", "Alt_L", "f") == ["fix_all"]
    assert presses("Super_L", "Alt_L", "space") == ["panel"]
    assert presses("Super_L", "f", wait=0.3) == []  # not the chord
    assert presses("Control_L", "Super_L", "Alt_L", "f", wait=0.3) == []  # an extra modifier


def test_chord_fires_with_numlock_and_capslock_on(keys):
    make, fired, presses = keys
    h = make()
    assert h.grab("pause", "Super+Alt+p", lambda: fired.put("pause"))
    for lock in ("Num_Lock", "Caps_Lock"):
        presses(lock, wait=0.1)  # on
        try:
            assert presses("Super_L", "Alt_L", "p") == ["pause"], lock
        finally:
            presses(lock, wait=0.1)  # off again


def test_grab_held_by_another_client_reports_conflict_and_recovers(keys):
    make, fired, presses = keys
    other, ours = make(), make()
    assert other.grab("x", "Super+Alt+z", lambda: fired.put("other"))
    assert ours.grab("revert", "Super+Alt+z", lambda: fired.put("ours")) is False
    assert ours.grabbed() == {}
    assert presses("Super_L", "Alt_L", "z") == ["other"]
    other.ungrab("x")
    assert ours.grab("revert", "Super+Alt+z", lambda: fired.put("ours")) is True
    assert presses("Super_L", "Alt_L", "z") == ["ours"]


def test_one_chord_cannot_be_bound_to_two_names(keys):
    make, fired, presses = keys
    h = make()
    assert h.grab("panel", "Super+Alt+space", lambda: fired.put("panel"))
    assert h.grab("pause", "alt+SUPER+space", lambda: fired.put("pause")) is False
    assert presses("Super_L", "Alt_L", "space") == ["panel"]


def test_regrab_under_the_same_name_moves_the_chord(keys):
    make, fired, presses = keys
    h = make()
    assert h.grab("panel", "Super+Alt+space", lambda: fired.put("old"))
    assert h.grab("panel", "Super+Alt+x", lambda: fired.put("new"))
    assert h.grabbed() == {"panel": "Super+Alt+x"}
    assert presses("Super_L", "Alt_L", "space", wait=0.3) == []
    assert presses("Super_L", "Alt_L", "x") == ["new"]


def test_escape_is_only_taken_while_grabbed(keys):
    make, fired, presses = keys
    h = make()
    assert presses("Escape", wait=0.3) == []
    assert h.grab("escape", "Escape", lambda: fired.put("esc"))
    assert presses("Escape") == ["esc"]
    h.ungrab("escape")
    h.ungrab("escape")  # harmless
    assert presses("Escape", wait=0.3) == [] and h.grabbed() == {}


def test_unknown_key_or_modifier_raises_value_error(keys):
    make, _fired, _presses = keys
    h = make()
    for bad in ("Super+Alt+NoSuchKey", "Hyper+x", "Super+Alt+"):
        with pytest.raises(ValueError):
            h.grab("bad", bad, lambda: None)
    assert h.grabbed() == {}


def test_bind_hotkeys_reports_a_real_conflict(keys):
    from glance.config import GeneralCfg
    from glance.ui.hotkeys import bind_hotkeys

    make, fired, presses = keys
    other, ours = make(), make()
    assert other.grab("x", "Super+Alt+f", lambda: fired.put("other"))
    chords = GeneralCfg().hotkeys
    actions = {name: (lambda n=name: fired.put(n)) for name in chords}
    assert bind_hotkeys(ours, chords, actions) == {"fix_all": "conflict"}
    assert sorted(ours.grabbed()) == sorted(n for n in chords if n != "fix_all")
    assert presses("Super_L", "Alt_L", "z") == ["revert"]


# ---------------------------------------------------------------- overlay


@pytest.fixture(scope="module")
def probe(xvfb, tmp_path_factory):
    """What overlay_probe.py saw. Its screenshots (collapsed and open, light and dark) go to
    $GLANCE_UI_SHOTS when that is set, else to a temp directory."""
    shots = Path(os.environ.get("GLANCE_UI_SHOTS") or tmp_path_factory.mktemp("shots"))
    shots.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "DISPLAY": xvfb, "NO_AT_BRIDGE": "1", "GDK_BACKEND": "x11"}
    env.pop("WAYLAND_DISPLAY", None)
    done = subprocess.run([sys.executable, str(PROBE), "--shots", str(shots)], env=env,
                          capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr[-2000:]
    out = json.loads(done.stdout.strip().splitlines()[-1])
    out["shots_dir"] = str(shots)
    return out


def test_overlay_windows_never_take_the_input_focus(probe):
    assert probe["focus_before"] is True
    assert probe["icon_override_redirect"] and probe["strip_override_redirect"]
    assert probe["panel_override_redirect"]
    # accept_focus, focus_on_map, is_active - for the icon, the strip and the drawer
    assert probe["focusable"] == [[False, False, False]] * 3
    assert probe["active_after_clicks"] == [False, False]
    assert probe["focus_after_icon_click"] and probe["focus_with_panel"]
    assert probe["focus_after_strip_clicks"] and probe["focus_after_panel_clicks"]


def test_status_icon_sits_at_the_right_end_of_the_caret_line(probe):
    # field (300, 270, 660, 70), caret (330, 300, 1, 18): inside the right edge, centred on
    # the caret line
    assert probe["icon"] == [960 - 22 - 4, 309 - 11, 22, 22]
    assert probe["icon_window"][:2] == [probe["icon"][0] - 1, probe["icon"][1] - 5]
    assert probe["icon_tail"][0] == 960 + 4  # text after the caret: outside the field
    assert probe["icon_moved"] == [probe["icon"][0] + 50, probe["icon"][1] + 20, 22, 22]
    assert probe["icon_hidden"] == [None, False, False]


def test_status_icon_states_and_its_click(probe):
    assert probe["icon_idle"] == {"state": "idle", "badge": None, "spinning": False}
    assert probe["icon_working"] == {"state": "working", "badge": None, "spinning": True}
    assert probe["icon_ready"] == {"state": "ready", "badge": "3", "spinning": False}
    assert probe["icon_muted"] == {"state": "muted", "badge": None, "spinning": False}
    assert probe["icon_click_events"] == [["chip_click"]]
    assert probe["icon_click_holder"] == 0  # the icon took the click...
    assert probe["icon_corner_click_holder"] == 1  # ...but only on its circle


def test_status_icon_toolbar(probe):
    items = ["Check", "Friendlier", "Direct", "Shorter", "Professional", "Rewrite…", "⋯"]
    assert probe["hover_items"] is None  # resting on the icon opens nothing
    assert probe["menu_items"] == items
    _x, y, w, h = probe["menu_geometry"]
    assert w > 3 * h and y >= probe["caret_bottom"]  # one slim row, under the caret line
    assert probe["menu_override_redirect"] is True and probe["focus_with_menu"]
    assert probe["menu_click_events"] == [["icon", {"key": "check"}]]
    assert probe["menu_after_click"] is None and probe["focus_after_menu_click"]
    # a right-click opens it at once (no click goes out), a second one closes it
    assert probe["rightclick_items"] == items
    assert probe["rightclick_events"] == [["icon", {"key": "check"}]]
    assert probe["rightclick_again"] is None and probe["focus_after_rightclick"]


def test_tone_preview_strip(probe):
    assert probe["tone_strip_text_light"].startswith("Hey team! I think")


def test_collapsed_strip_shows_only_the_corrected_sentence(probe):
    assert probe["strip_text"] == ("Hey team I think it's fine to ship Friday, but we need "
                                   "the tests fixed ASAP.")
    assert probe["strip_buttons"] == ["Alt+Enter", "⋯"]
    # under the caret line, aligned with the field and as wide as it
    assert probe["strip"] == [300, 324, 660, probe["strip"][3]] == probe["strip_reported"]
    assert 30 <= probe["strip"][3] <= 80
    assert probe["note_buttons"] == ["⋯"]  # a tone note: nothing to use
    assert probe["note_text"].startswith("Reads blunt for team channel")


def test_strip_lets_clicks_through_except_on_its_buttons(probe):
    assert probe["click_through_sentence"] == 1
    assert probe["clicks_on_strip_reach_holder"] == 0
    assert probe["strip_events"] == [["use", {}], ["more", {}]]


def test_strip_flips_above_the_line_near_the_bottom(probe):
    x, y, _w, h = probe["strip_low"]
    assert y + h <= 770 and x == 300  # never over the line being typed


def test_drawer_opens_in_the_strips_place(probe):
    # The open panel shows the corrected text itself ("Suggested"): the strip is not shown
    # on its own, and the panel takes its place under the caret line.
    assert probe["strip_open"] is None
    sx, sy, sw, _sh = probe["strip"]
    px, py, pw, ph = probe["panel"]
    left, top, _right, _bottom = probe["consts"]["shadow"]
    assert (px + left, py + top) == (sx, sy)
    assert pw >= sw and 200 < ph < 480
    assert probe["panel"] == probe["panel_reported"]
    assert probe["drawer_buttons"][:8] == [
        "For Team channel ▾", "Esc | Close", "Apply (Alt+Enter)",
        "Friendlier | Could someone pick this up? Thanks! | 1",
        "More direct | Ship Friday once tests pass. | 2", "Shorter | Click to preview | 3",
        "Professional | Rewriting… | 4", "Rewrite as… | e.g. make it a bullet list | ↵"]
    # the same six issues as the strip's sentence: count and fixes agree with it
    assert probe["drawer_buttons"][8:] == ["6 fixes", "hey → Hey", "i → I", "its → it's",
                                           "+3 more", "Fix All"]


def test_a_selected_tone_shows_its_rewrite_in_suggested(probe):
    buttons = probe["selected_buttons"]
    assert buttons[2] == "Apply (Alt+Enter)"  # applies the selected rewrite
    assert buttons[3].startswith("Friendlier | Hey team! I think it's fine")
    assert "Shorter | Click to preview | 3" in buttons


def test_drawer_clicks_reach_the_callbacks(probe):
    assert "missing_buttons" not in probe
    first = "harper:SentenceCapitalization:0:3"
    friday = "harper:SpellCheck:34:40"
    assert probe["drawer_events"] == [
        ["tone", {"key": "shorter"}],  # a click selects; nothing happens on hover
        ["tone", {"key": "custom"}],
        ["fix_all", {}],
        ["accept", {"issue_id": first, "choice": 0}],
        ["accept", {"issue_id": "harper:ItsContraction:17:20", "choice": 0}],
        ["ignore", {"issue_id": first, "scope": "app"}],
        ["add_dict", {"issue_id": friday}],
        ["accept", {"issue_id": friday, "choice": 1}],
        ["profile", {"name": "email"}],
        ["close", {}],
    ]
    # "6 fixes" opened every fix with its other choices
    assert {"Friday,", "Fridays", "Add to dictionary", "Ignore here", "Ignore everywhere"} <= set(
        probe["details_buttons"])


def test_selection_panel_and_an_empty_drawer(probe):
    x, y, w, h = probe["selection"]
    assert (x + w, y + h) == (1280, 800)  # clamped to the monitor
    assert probe["selection_buttons"][-2:] == ["Paste over selection", "Copy rewrite"]
    assert "Send" not in probe["selection_buttons"]
    assert probe["empty_panel"][3] < 120


def test_strip_moves_fast_and_hides(probe):
    assert probe["move_median_s"] < 0.05
    assert probe["hidden"] == [None, None, None, False, False]
    assert probe["clipboard"] == "copied by the probe"


def test_dark_theme_and_screenshots(probe):
    assert probe["dark"] is True
    shots = Path(probe["shots_dir"])
    states = [f"icon-{s}" for s in ("idle", "working", "ready", "tone", "muted")]
    states += ["muted-strip", "toolbar", "toolbar-muted", "tone-loading", "tone-strip", "strip", "panel", "panel-selected", "panel-working",
               "selection", "ghost", "shortcuts"]
    for state in states:
        for theme in ("light", "dark"):
            name = f"v3-{state}-{theme}"
            assert (shots / f"{name}.png").stat().st_size > 2000, name


# ---------------------------------------------------------------- the whole daemon

HARPER = Path.home() / ".local/share/glance/bin/harper-ls"


def _wait(pred, timeout: float, step: float = 0.05):
    deadline = time.monotonic() + timeout
    while True:
        value = pred()
        if value or time.monotonic() > deadline:
            return value
        time.sleep(step)


@pytest.fixture(scope="module")
def daemon(xvfb, tmp_path_factory):
    """glanced on the Xvfb display with its own D-Bus session (so its capture helper sees
    an empty accessibility bus, never the user's applications) and temp XDG directories.
    It starts with an invalid config.toml. Grammar runs on the harper-ls child (these tests
    supervise it), even where the in-process engine is installed."""
    with _glanced(xvfb, tmp_path_factory, "harper-ls") as d:
        yield d


@pytest.fixture(scope="module")
def native_daemon(xvfb, tmp_path_factory):
    """The same glanced with grammar in its own process (glance_core)."""
    from glance.grammar.harper_native import available

    if not available():
        pytest.skip("glance_core is not built")
    with _glanced(xvfb, tmp_path_factory, "native") as d:
        yield d


@contextlib.contextmanager
def _glanced(xvfb, tmp_path_factory, grammar: str):
    if shutil.which("dbus-run-session") is None:
        pytest.skip("dbus-run-session is not installed")
    if not HARPER.exists():
        pytest.skip("harper-ls is not installed: run scripts/fetch-harper.sh")
    from glance.ipc import JsonLineClient

    home = tmp_path_factory.mktemp("d")
    dirs = {name: home / name for name in ("rt", "cfg", "data")}
    for d in dirs.values():
        d.mkdir(mode=0o700)
    (dirs["data"] / "glance" / "bin").mkdir(parents=True)
    (dirs["data"] / "glance" / "bin" / "harper-ls").symlink_to(HARPER)
    config = dirs["cfg"] / "glance" / "config.toml"
    config.parent.mkdir()
    config.write_text("[general]\nchip_idle_ms = \"soon\"\n")
    # PATH holds only the D-Bus tools: no vendor CLI is found, so the onboarding window's
    # health checks never run the user's real `claude auth status` and friends.
    tools = home / "bin"
    tools.mkdir()
    for name in ("dbus-run-session", "dbus-daemon"):
        (tools / name).symlink_to(shutil.which(name) or f"/usr/bin/{name}")
    env = {**os.environ, "DISPLAY": xvfb, "NO_AT_BRIDGE": "1", "GDK_BACKEND": "x11",
           "XDG_SESSION_TYPE": "x11", "PATH": str(tools),
           "XDG_RUNTIME_DIR": str(dirs["rt"]), "XDG_CONFIG_HOME": str(dirs["cfg"]),
           "XDG_DATA_HOME": str(dirs["data"]), "GLANCE_STRICT_LOG": "1",
           "GLANCE_GRAMMAR": grammar}
    for name in ("WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "AT_SPI_BUS_ADDRESS"):
        env.pop(name, None)
    log_path = home / "glanced.stderr"
    sock = dirs["rt"] / "glance" / "ctl.sock"

    def request(msg_type: str, payload: dict | None = None) -> dict | None:
        client = JsonLineClient(sock)
        try:
            client.connect(timeout_s=0)
            return client.request(msg_type, payload or {})
        except (ConnectionError, TimeoutError):
            return None
        finally:
            client.close()

    def glance(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "glance.cli", *args], env=env,
                              capture_output=True, text=True, timeout=10, check=False)

    def children(pattern: str) -> list[int]:
        pid = request("ping")["pid"]
        done = subprocess.run(["pgrep", "-P", str(pid), "-f", pattern], capture_output=True,
                              text=True, check=False)
        return [int(p) for p in done.stdout.split()]

    with log_path.open("w") as log:
        proc = subprocess.Popen(
            ["dbus-run-session", "--", sys.executable, "-m", "glance.daemon.main"],
            env=env, stderr=log, stdout=subprocess.DEVNULL)
        try:
            assert _wait(lambda: (request("status") or {}).get("capture_connected"), 15), \
                log_path.read_text()[-2000:]
            yield SimpleNamespace(request=request, glance=glance, children=children,
                                  config=config, env=env, proc=proc, dirs=dirs,
                                  log_path=log_path, sock=sock, display=xvfb)
        finally:
            if proc.poll() is None:
                pid = (request("ping") or {}).get("pid")
                if pid:
                    os.kill(pid, 15)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()


def test_daemon_reports_an_invalid_startup_config_and_recovers(daemon):
    st = daemon.request("status")
    assert st["config_ok"] is False
    log = daemon.log_path.read_text()
    assert "config_invalid startup=True" in log and log.count("config_invalid") == 1
    daemon.config.write_text("[general]\nchip_idle_ms = 300\n")
    assert _wait(lambda: daemon.request("status")["config_ok"], 3.0)


def test_daemon_status_and_sockets(daemon):
    # harper-ls is ready a moment after capture connects (its start warms up the speller)
    assert _wait(lambda: daemon.request("status")["harper_alive"], 10.0)
    st = daemon.request("status")
    assert st["ok"] and st["harper_alive"] and st["capture_connected"]
    assert (st["capture_child"], st["harper_child"]) == ("up", "up")
    assert st["grammar_engine"] == "harper-ls"
    want = "check,complete,fix_all,panel,pause,revert,selection,shot,tone1,tone2,tone3,tone4"
    assert _wait(lambda: daemon.request("status")["hotkeys_grabbed"] == want, 5.0)
    st = daemon.request("status")
    assert st["hotkeys_unavailable"] is None
    assert st["field"] is None and st["paused"] is False
    runtime = daemon.sock.parent
    assert oct(runtime.stat().st_mode & 0o777) == "0o700"
    for name in ("ctl.sock", "capture.sock"):
        assert oct((runtime / name).stat().st_mode & 0o777) == "0o600"


def test_daemon_cli_pause_and_resume(daemon):
    assert daemon.glance("pause", "1m").returncode == 0
    assert "paused: true" in daemon.glance("status").stdout.splitlines()
    assert 0 < daemon.request("status")["pause_remaining_s"] <= 60
    assert daemon.glance("resume").returncode == 0
    assert "paused: false" in daemon.glance("status").stdout.splitlines()


def test_daemon_pause_hotkey(daemon):
    keyboard = Keyboard(daemon.display)
    try:
        keyboard.chord("Super_L", "Alt_L", "p")
        assert _wait(lambda: daemon.request("status")["paused"], 2.0)
        keyboard.chord("Super_L", "Alt_L", "p")
        assert _wait(lambda: not daemon.request("status")["paused"], 2.0)
    finally:
        keyboard.close()


def test_second_daemon_exits_1(daemon):
    done = subprocess.run([sys.executable, "-m", "glance.daemon.main"], env=daemon.env,
                          capture_output=True, text=True, timeout=20, check=False)
    assert done.returncode == 1 and done.stderr.strip() == "glanced is already running"
    assert daemon.request("ping")["ok"]  # the first one is untouched


def test_daemon_restarts_a_killed_capture_within_2s(daemon):
    (pid,) = daemon.children("glance.capture.main")
    os.kill(pid, 15)
    t0 = time.monotonic()
    assert _wait(lambda: not daemon.request("status")["capture_connected"], 2.0, 0.01)
    assert _wait(lambda: daemon.request("status")["capture_connected"], 5.0, 0.01)
    assert time.monotonic() - t0 < 2.0
    assert daemon.children("glance.capture.main") != [pid]


def test_daemon_restarts_a_killed_harper_and_is_degraded_meanwhile(daemon):
    (pid,) = daemon.children("harper-ls")
    os.kill(pid, 15)
    assert _wait(lambda: daemon.request("status")["harper_alive"] is False, 2.0, 0.01)
    # harper-ls is only the fallback engine now; its respawn + dictionary warm-up takes ~6 s
    assert _wait(lambda: daemon.request("status")["harper_alive"] is True, 10.0)
    assert daemon.children("harper-ls") not in ([], [pid])


def test_daemon_runs_grammar_in_process(native_daemon):
    d = native_daemon
    assert _wait(lambda: d.request("status")["harper_alive"], 10.0)
    st = d.request("status")
    assert st["grammar_engine"] == "native" and st["harper_child"] == "up"
    assert d.children("harper-ls") == []
    pid = d.request("ping")["pid"]
    os.kill(pid, 15)
    assert d.proc.wait(timeout=10) == 0
    log = d.log_path.read_text()
    assert "Traceback" not in log and "grammar_native_error" not in log


def test_daemon_stops_cleanly_on_sigterm(daemon):
    pid = daemon.request("ping")["pid"]
    kids = daemon.children("glance.capture.main") + daemon.children("harper-ls")
    assert len(kids) == 2
    os.kill(pid, 15)
    assert daemon.proc.wait(timeout=10) == 0
    assert not daemon.sock.exists() and not (daemon.sock.parent / "capture.sock").exists()
    assert _wait(lambda: not any(Path(f"/proc/{k}").exists() for k in kids), 3.0)
    assert daemon.glance("status").returncode == 3
    log = daemon.log_path.read_text()
    assert "Traceback" not in log and "glanced_stopping" in log


# ---------------------------------------------------------------- selection actions

SEL_PROBE = Path(__file__).parent / "desktop" / "selection_probe.py"


def test_selection_mark_bar_and_x11_watcher(xvfb, tmp_path):
    """The mark and the bar are click-through popups that never take the focus; the XFixes
    + raw-button watcher settles a mouse selection, clears a keyboard one, and reports keys
    only while armed. Screenshots go to $GLANCE_UI_SHOTS (sel-*.png) when it is set."""
    shots = Path(os.environ.get("GLANCE_UI_SHOTS") or tmp_path)
    env = {**os.environ, "DISPLAY": xvfb, "NO_AT_BRIDGE": "1", "GDK_BACKEND": "x11"}
    env.pop("WAYLAND_DISPLAY", None)
    done = subprocess.run([sys.executable, str(SEL_PROBE), "--shots", str(shots)], env=env,
                          capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr[-2000:]
    out = json.loads(done.stdout.strip().splitlines()[-1])
    assert out["mark_override_redirect"] and out["focus_kept"] and out["hidden"]
    assert out["mark_hit"] == [True, False]
    assert out["mark_click"] == ["sel_mark"]
    assert out["bar_click"] == [["sel_action", {"key": "explain"}]]
    assert out["watch_running"] and out["settled_early"] == []
    assert out["mouse"] == [["settled", 520, 210]]
    assert out["keyboard"] == [["cleared"]]
    assert out["key_unarmed"] == [] and out["armed"] == [["input", "key"], ["input", "scroll"]]
