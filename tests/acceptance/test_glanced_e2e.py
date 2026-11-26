"""The shipped glanced, end to end: glanced + glance-capture + harper-ls + the test AI backend
against a GTK text view, on a private Xvfb with its own D-Bus and AT-SPI bus (the acceptance
`session`), never the user's display. Keys go in through XTEST on that server only; what
Glance did is read back through `glance status`, the GLANCE_TEST ctl hooks and the app's own
buffer.

    .venv/bin/pytest -m xvfb tests/acceptance/test_glanced_e2e.py

The tests share one daemon and run in file order (~1 min).
"""
from __future__ import annotations

import json
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.acceptance.harness import GTK_APP, Typist

HARPER = Path.home() / ".local/share/glance/bin/harper-ls"
TEXT = "Teh quick brown fox jumps over the lazy dog."
REWRITE = "Thanks so much, will do!"  # FakeAiBackend.REWRITE
GUARDED = "hey team, see attached for the numbers from today"
EDIT_TITLE = "Glance — edit prompt"

pytestmark = [
    pytest.mark.xvfb,
    pytest.mark.skipif(not HARPER.exists(), reason="harper-ls is not installed"),
]


def wait_for(pred, timeout_s: float, step_s: float = 0.03):
    """The predicate's last value: falsy when it timed out (so the assert can say why)."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            value = pred()
        except (queue.Empty, KeyError, TypeError):
            value = None
        if value or time.monotonic() > deadline:
            return value
        time.sleep(step_s)


def _dirs(root: Path) -> dict[str, Path]:
    dirs = {n: root / n for n in ("rt", "cfg", "data")}
    for d in dirs.values():
        d.mkdir(mode=0o700, parents=True)
    (dirs["data"] / "glance" / "bin").mkdir(parents=True)
    (dirs["data"] / "glance" / "bin" / "harper-ls").symlink_to(HARPER)
    return dirs


class Glanced:
    """One real glanced in the private session, with temp XDG directories."""

    def __init__(self, session, root: Path, fake_ai: bool) -> None:
        self.dirs = _dirs(root)
        self.env = {**session.env, "XDG_RUNTIME_DIR": str(self.dirs["rt"]),
                    "XDG_CONFIG_HOME": str(self.dirs["cfg"]),
                    "XDG_DATA_HOME": str(self.dirs["data"]), "GLANCE_STRICT_LOG": "1",
                    "GLANCE_TEST": "1"}
        if fake_ai:
            self.env["GLANCE_FAKE_BACKEND"] = "1"
        else:  # no AI anywhere: no vendor CLI on PATH; a keyring would live in the temp
            self.env["PATH"] = "/nonexistent"  # XDG_DATA_HOME, never the user's
        self.log_path = root / "glanced.stderr"
        self.sock = self.dirs["rt"] / "glance" / "ctl.sock"
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "glance.daemon.main"],
            env={**self.env, "NO_AT_BRIDGE": "1"},  # its own windows stay off the a11y bus
            stderr=self.log_path.open("w"), stdout=subprocess.DEVNULL)

    def request(self, msg_type: str, payload: dict | None = None,
                timeout_s: float = 5.0) -> dict:
        from glance.ipc import JsonLineClient

        client = JsonLineClient(self.sock)
        try:
            client.connect(timeout_s=0)
            return client.request(msg_type, payload or {}, timeout_s=timeout_s)
        except (ConnectionError, TimeoutError):
            return {}
        finally:
            client.close()

    def status(self) -> dict:
        return self.request("status")

    def state(self) -> dict:
        return self.request("test_state")

    def glance(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "glance.cli", *args], env=self.env,
                              capture_output=True, text=True, timeout=60, check=False)

    def log(self) -> str:
        return self.log_path.read_text()

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()  # a process of this private session


class App:
    """gtk_app.py as Slack (a chat app: the send-guard's ground), a GtkSourceView."""

    def __init__(self, session) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(GTK_APP), "--text", TEXT, "--undo", "--wm-class", "Slack"],
            env=session.env, stdout=subprocess.PIPE, text=True)
        self.lines: queue.Queue = queue.Queue()
        threading.Thread(target=lambda: [self.lines.put(x.rstrip("\n"))
                                         for x in self.proc.stdout], daemon=True).start()
        assert wait_for(lambda: self.lines.get(timeout=15) == "READY", 20.0), "app not ready"

    def buffer(self) -> str:
        while not self.lines.empty():
            self.lines.get_nowait()
        self.proc.send_signal(signal.SIGUSR1)
        return json.loads(self.lines.get(timeout=5))

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            self.proc.wait(timeout=10)


def clipboard(session) -> str:
    code = ("import gi; gi.require_version('Gtk', '3.0'); gi.require_version('Gdk', '3.0'); "
            "from gi.repository import Gdk, Gtk; "
            "print(Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).wait_for_text() or '')")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env={**session.env, "NO_AT_BRIDGE": "1"}, timeout=15, check=False)
    return done.stdout.rstrip("\n")


def window_titles(session, pid: int) -> list[str]:
    """_NET_WM_NAME of the mapped top-level windows of `pid` on the private display."""
    from Xlib import X, display, error

    d = display.Display(session.display)
    try:
        atom = d.intern_atom
        out = []
        for w in d.screen().root.query_tree().children:
            try:
                owner = w.get_full_property(atom("_NET_WM_PID"), X.AnyPropertyType)
                name = w.get_full_property(atom("_NET_WM_NAME"), atom("UTF8_STRING"))
                if owner and int(list(owner.value)[0]) == pid and name \
                        and w.get_attributes().map_state == X.IsViewable:
                    v = name.value
                    out.append(v.decode() if isinstance(v, bytes) else str(v))
            except error.XError:
                continue
        return out
    finally:
        d.close()


@pytest.fixture(scope="module")
def rig(session, tmp_path_factory):
    if shutil.which("dbus-daemon") is None:
        pytest.skip("dbus-daemon is missing")
    app = App(session)
    daemon = Glanced(session, tmp_path_factory.mktemp("glanced"), fake_ai=True)
    typist = Typist(session)
    try:
        assert wait_for(lambda: (daemon.status().get("field") or {}).get("len") == len(TEXT)
                        and daemon.status().get("ai") == "ok", 30.0), \
            (daemon.status(), daemon.log()[-3000:])
        yield SimpleNamespace(session=session, app=app, daemon=daemon, typist=typist)
    finally:
        typist.close()
        daemon.stop()
        app.stop()


def set_text(rig, text: str) -> None:
    """Replace the whole field through glanced's own verified write-back (a test hook)."""
    have = rig.app.buffer()
    assert wait_for(lambda: rig.daemon.state()["snapshot"]["text"] == have, 5.0)

    def apply() -> bool:  # refused while an earlier apply is still being verified
        return rig.daemon.request("test_apply", {"start": 0, "end": len(have),
                                                 "replacement": text, "method": "auto"})["ok"]

    assert wait_for(apply, 5.0, 0.3)
    assert wait_for(lambda: rig.app.buffer() == text, 5.0), rig.app.buffer()
    assert wait_for(lambda: (rig.daemon.state().get("snapshot") or {}).get("text") == text, 5.0)


def chip(rig) -> dict:
    return rig.daemon.state().get("chip", {})


def test_status_reports_every_hotkey_grabbed(rig):
    want = {"check", "complete", "fix_all", "panel", "pause", "revert", "selection",
            "tone1", "tone2", "tone3", "tone4"}
    assert wait_for(lambda: set(rig.daemon.status().get("hotkeys_grabbed", "").split(","))
                    == want, 5.0), rig.daemon.status()
    done = rig.daemon.glance("status", "--json")
    st = json.loads(done.stdout)
    assert set(st["hotkeys_grabbed"].split(",")) == want and st["hotkeys_unavailable"] is None


def test_nothing_is_checked_until_the_check_hotkey(rig):
    # `[general] check = "shortcut"` (the default): typing alone checks nothing.
    time.sleep(1.5)
    assert rig.daemon.state()["issues"] == [] and not rig.daemon.state()["strip"]
    rig.typist.chord("Super_L", "Alt_L", "g")
    assert wait_for(lambda: rig.daemon.state()["strip"] and rig.daemon.state()["issues"],
                    10.0), json.dumps([rig.daemon.state(), rig.daemon.status()])  # "Teh"
    rig.daemon.request("test_panel_action", {"action": "close", "args": {}})
    assert wait_for(lambda: not rig.daemon.state()["strip"], 5.0)


def test_grammar_chip_appears(rig):
    assert wait_for(lambda: chip(rig).get("state") == "fixes", 10.0), \
        json.dumps([rig.daemon.state()["chip"], rig.daemon.status()])
    assert int(chip(rig)["label"]) >= 1  # "Teh"


def test_tone_hotkey_rewrites_the_field(rig):
    rig.typist.chord("Super_L", "Alt_L", "1")
    assert wait_for(lambda: rig.app.buffer() == REWRITE, 5.0), rig.app.buffer()
    set_text(rig, TEXT)


def test_autocomplete_ghost_and_tab(rig):
    rig.typist.chord("Control_L", "End")
    assert wait_for(lambda: rig.daemon.state()["snapshot"]["caret"] == len(TEXT), 5.0)
    rig.typist.chord("Super_L", "Alt_L", "Return")
    assert wait_for(lambda: rig.daemon.status().get("autocomplete_visible") is True, 5.0), \
        rig.daemon.log()[-2000:]
    # The hotkey's grab blurred the app until the keys were released: the field is back.
    assert wait_for(lambda: rig.daemon.status().get("field") is not None, 5.0)
    assert rig.daemon.status().get("autocomplete_visible") is True
    rig.typist.key("Tab")
    assert wait_for(lambda: rig.app.buffer().endswith("tomorrow morning."), 5.0), \
        rig.app.buffer()
    assert rig.app.buffer().startswith(TEXT)
    assert wait_for(lambda: rig.daemon.status().get("autocomplete_visible") is False, 5.0)


def test_tab_without_a_ghost_reaches_the_app(rig):
    # No suggestion on screen, so Tab is not grabbed: GTK moves the focus to the next field
    # (the empty entry), which capture reports.
    assert rig.daemon.status().get("autocomplete_visible") is False
    rig.typist.key("Tab")
    assert wait_for(lambda: (rig.daemon.status().get("field") or {}).get("len") == 0, 5.0), \
        rig.daemon.status()
    rig.typist.chord("Shift_L", "Tab")
    assert wait_for(lambda: (rig.daemon.status().get("field") or {}).get("len", 0) > 0, 5.0)
    set_text(rig, TEXT)


def test_send_guard_chip_for_see_attached(rig):
    set_text(rig, GUARDED)
    assert wait_for(lambda: chip(rig).get("state") == "guard", 5.0), rig.daemon.state()
    assert chip(rig)["label"] == "Attachment mentioned — attached?"
    set_text(rig, TEXT)
    assert wait_for(lambda: chip(rig).get("state") != "guard", 5.0)


def test_select_anything_copies_a_rewrite(rig):
    rig.typist.chord("Control_L", "a")  # the app now owns PRIMARY
    time.sleep(0.3)
    rig.typist.chord("Super_L", "Alt_L", "a")
    assert wait_for(lambda: rig.daemon.status().get("selection_open") is True, 5.0), \
        rig.daemon.log()[-2000:]
    rig.daemon.request("test_panel_action", {"action": "sel_copy", "args": {}})
    assert wait_for(lambda: clipboard(rig.session) == REWRITE, 10.0, 0.3)
    assert rig.daemon.status().get("selection_open") is False
    assert rig.app.buffer() == TEXT  # Copy never touches the field


def test_glance_edit_round_trip(rig):
    got: list[dict] = []
    t = threading.Thread(target=lambda: got.append(rig.daemon.request(
        "edit_session", {"text": "fix teh bug", "window": 0}, timeout_s=30.0)), daemon=True)
    t.start()
    pid = rig.daemon.request("ping")["pid"]
    assert wait_for(lambda: EDIT_TITLE in window_titles(rig.session, pid), 10.0), \
        rig.daemon.log()[-2000:]
    time.sleep(0.5)
    rig.typist.chord("Control_L", "End")
    rig.typist.type(" now")
    rig.typist.chord("Control_L", "Return")
    t.join(15)
    assert got and got[0].get("action") == "apply" and got[0]["text"] == "fix teh bug now", got


def test_nothing_typed_reaches_the_disk_or_the_log(rig):
    paths = [str(d) for d in rig.daemon.dirs.values()] + [str(rig.daemon.log_path)]
    for needle in ("quick brown fox", "Thanks so much", "see attached", "fix teh bug",
                   "tomorrow morning"):
        done = subprocess.run(["grep", "-r", "-l", needle, *paths], capture_output=True,
                              text=True, check=False)
        assert done.returncode == 1 and done.stdout == "", (needle, done.stdout)
    assert "Traceback" not in rig.daemon.log()


def test_no_backend_lists_and_never_opens_onboarding_by_itself(rig, tmp_path_factory):
    rig.daemon.stop()  # one glanced per display: the hotkeys are the first one's otherwise
    bare = Glanced(rig.session, tmp_path_factory.mktemp("bare"), fake_ai=False)
    try:
        assert wait_for(lambda: bare.status().get("capture_connected"), 20.0), bare.log()[-2000:]
        done = bare.glance("backend", "list")
        assert done.returncode == 0, done.stderr
        assert done.stdout.startswith("No AI connected"), done.stdout
        assert "claude" in done.stdout and "(in use)" not in done.stdout
        listed = bare.request("backend_list", {}, timeout_s=30.0)
        assert listed["ok"] and listed["choice"] is None
        assert not any(b["signed_in"] for b in listed["backends"].values()), listed
        assert wait_for(lambda: bare.status().get("ai") == "none", 10.0), bare.status()
        # "No AI connected" is the tray's state; the window opens only from the tray menu
        assert "Connect an AI" not in window_titles(rig.session, bare.proc.pid)
    finally:
        bare.stop()
    assert "Traceback" not in bare.log()
