"""Acceptance harness: fixture pages, real browsers on a private X server, XTEST typing, and
the capture process as the unit under test.

Nothing here touches the user's display, browser profiles or accessibility bus: `Private
Session` starts its own Xvfb, session D-Bus (whose AT-SPI bus is launched on demand) and a
minimal window manager, and every child process gets that environment.
"""
from __future__ import annotations

import json
import os
import queue
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures"
GTK_APP = REPO / "tests" / "desktop" / "gtk_app.py"
MINI_WM = REPO / "tests" / "desktop" / "mini_wm.py"
QUIET_S = 0.12
CMD_POLL_S = 20.0

BROWSERS = {
    "chrome": "/usr/bin/google-chrome-stable",
    "brave": "/usr/bin/brave-browser-nightly",
    # GLANCE_FIREFOX_BIN: a Firefox that is not the snap (see NO_GO in the matrix test).
    "firefox": os.environ.get("GLANCE_FIREFOX_BIN", "/usr/bin/firefox"),
}


def norm(s: str) -> str:
    """The comparison normalisation of `WriteBackEngine`: NFC, NBSP as space, one trailing
    newline dropped."""
    s = unicodedata.normalize("NFC", s).replace(" ", " ")
    return s.removesuffix("\n")


def wait_for(pred: Callable[[], Any], timeout_s: float, step_s: float = 0.02) -> Any:
    deadline = time.monotonic() + timeout_s
    while True:
        value = pred()
        if value:
            return value
        if time.monotonic() > deadline:
            raise TimeoutError
        time.sleep(step_s)


# ------------------------------------------------------------------------ edit script

_BASE = ["the", "quick", "brown", "fox", "jumps", "over", "lazy", "dog", "and", "writes", "teh", "letter", "about", "recieve", "plans", "meeting", "tomorrow", "morning", "please", "review", "draft", "report", "budget", "schedule", "team", "update", "thanks", "regards", "kind", "hello", "world", "cafe", "naive", "resume", "coordinate", "client", "invoice", "launch", "policy", "answer", "question", "sentence", "paragraph", "editor", "window", "keyboard", "cursor", "typing", "silent", "garden"]
SUFFIXES = ("", ",", ".", "!")
WORDS: tuple[str, ...] = (
    tuple(w + s for w in _BASE[:49] for s in SUFFIXES) + ("don't", "it's", "\U0001f600",
                                                           "\U0001f680"))
assert len(WORDS) == 200


@dataclass(frozen=True)
class Op:
    kind: str  # type backspace arrow home_end set_caret paste select_type
    text: str = ""  # the word, the key name, or "" ...
    n: int = 0
    frac: float = 0.0  # set_caret: position as a fraction of the current text length


def edit_script(seed: int, n: int = 200) -> list[Op]:
    """A deterministic mix of edits (see the plan for the percentages)."""
    rng = random.Random(seed)
    ops: list[Op] = []
    for _ in range(n):
        r = rng.random()
        if r < 0.45:
            ops.append(Op("type", rng.choice(WORDS) + " "))
        elif r < 0.60:
            ops.append(Op("backspace", n=rng.randint(1, 5)))
        elif r < 0.70:
            ops.append(Op("arrow", rng.choice(("Left", "Right")), rng.randint(1, 8)))
        elif r < 0.80:
            ops.append(Op("home_end", rng.choice(("Home", "End"))))
        elif r < 0.88:
            ops.append(Op("set_caret", frac=rng.random()))
        elif r < 0.95:
            ops.append(Op("paste", rng.choice(WORDS)))
        else:
            ops.append(Op("select_type", rng.choice(WORDS), rng.randint(1, 6)))
    return ops


# ------------------------------------------------------------------------ fixture server


class FixtureServer:
    """Serves the fixture pages and is the pages' side of the contract: `POST /state`
    receives {text, caret, seq}; `GET /cmd` long-polls for the next command."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve()
        self._state: dict[str, Any] = {}
        self._cond = threading.Condition()
        self._cmds: queue.Queue = queue.Queue()
        self._httpd: ThreadingHTTPServer | None = None
        self.posts = 0

    def start(self) -> str:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                if path == "/cmd":
                    try:
                        cmd = server._cmds.get(timeout=CMD_POLL_S)
                    except queue.Empty:
                        cmd = {"op": "noop"}
                    try:
                        self._send(200, json.dumps(cmd).encode())
                    except OSError:
                        server._cmds.put(cmd)  # the page went away; keep it for the next
                    return
                target = (server._root / path.lstrip("/")).resolve()
                if target.is_dir():
                    target = target / "index.html"
                if server._root not in target.parents or not target.is_file():
                    self._send(404, b"not found", "text/plain")
                    return
                ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript",
                         ".css": "text/css"}.get(target.suffix, "application/octet-stream")
                self._send(200, target.read_bytes(), ctype)

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path == "/state":
                    state = json.loads(body)
                    with server._cond:
                        server._state = state
                        server.posts += 1
                        server._cond.notify_all()
                self._send(204, b"")

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def state(self) -> dict[str, Any]:
        with self._cond:
            return dict(self._state)

    def wait_state(self, pred: Callable[[dict[str, Any]], bool],
                   timeout_s: float = 10.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        with self._cond:
            while not pred(self._state):
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError
                self._cond.wait(left)
            return dict(self._state)

    def cmd(self, op: str, **kw: Any) -> None:
        self._cmds.put({"op": op, **kw})

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


# ------------------------------------------------------------------------ private desktop


class PrivateSession:
    """Xvfb + session bus + minimal WM. `env` is for every process that runs inside."""

    def __init__(self) -> None:
        self.procs: list[subprocess.Popen] = []
        self.display = ""
        self.env: dict[str, str] = {}
        self.a11y_address = ""
        self._home = Path(tempfile.mkdtemp(prefix="glance-xvfb-"))

    def start(self) -> PrivateSession:
        try:
            return self._start()
        except BaseException:
            self.stop()  # never leave a private server behind
            raise

    def _start(self) -> PrivateSession:
        r, w = os.pipe()
        xvfb = subprocess.Popen(
            ["Xvfb", "-displayfd", str(w), "-screen", "0", "1280x900x24", "-nolisten", "tcp"],
            pass_fds=[w], stderr=subprocess.DEVNULL)
        os.close(w)
        self.procs.append(xvfb)
        with os.fdopen(r) as f:
            number = f.readline().strip()
        if not number:
            raise RuntimeError("Xvfb did not start")
        self.display = f":{number}"
        base = {k: v for k, v in os.environ.items()
                if k not in ("WAYLAND_DISPLAY", "AT_SPI_BUS_ADDRESS", "NO_AT_BRIDGE",
                             "DBUS_SESSION_BUS_ADDRESS")}
        (self._home / "Xauthority").touch()  # Xvfb has no cookie; Xlib wants a readable file
        base.update(DISPLAY=self.display, XAUTHORITY=str(self._home / "Xauthority"), XDG_SESSION_TYPE="x11", GDK_BACKEND="x11",
                    GNOME_ACCESSIBILITY="1", GTK_MODULES="gail:atk-bridge",
                    XDG_CONFIG_HOME=str(self._home / "config"),
                    XDG_DATA_HOME=str(self._home / "data"),
                    GLANCE_CLI_SEARCH="path")  # never find the developer's real AI CLIs
        for d in ("config", "data"):
            (self._home / d).mkdir(exist_ok=True)
        r, w = os.pipe()
        bus = subprocess.Popen(
            ["dbus-daemon", "--session", "--nofork", f"--print-address={w}"],
            pass_fds=[w], env=base, stderr=subprocess.DEVNULL)
        os.close(w)
        self.procs.append(bus)
        with os.fdopen(r) as f:
            address = f.readline().strip()
        base["DBUS_SESSION_BUS_ADDRESS"] = address
        self.env = base
        # Wake the accessibility bus (launched on demand) and say accessibility is on.
        subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.a11y.Bus", "--object-path",
             "/org/a11y/bus", "--method", "org.freedesktop.DBus.Properties.Set",
             "org.a11y.Status", "IsEnabled", "<true>"],
            env=base, capture_output=True, timeout=20, check=False)
        got = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.a11y.Bus", "--object-path",
             "/org/a11y/bus", "--method", "org.a11y.Bus.GetAddress"],
            env=base, capture_output=True, text=True, timeout=20, check=False).stdout
        self.a11y_address = got.split("'")[1] if "'" in got else ""
        wm = subprocess.Popen([sys.executable, str(MINI_WM)], env=base, stderr=None,
                              stdout=subprocess.PIPE, text=True)
        self.procs.append(wm)
        assert wm.stdout is not None
        for line in wm.stdout:  # Xlib may print an xauth warning first
            if line.strip() == "READY":
                break
        else:
            raise RuntimeError("mini_wm did not start")
        return self

    def stop(self) -> None:
        for p in reversed(self.procs):
            if p.poll() is None:
                p.terminate()
        for p in self.procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()  # a process of this private session, never the user's
        shutil.rmtree(self._home, ignore_errors=True)


# ------------------------------------------------------------------------ browsers


class Browser:
    """One browser on a throwaway profile, in the private session."""

    def __init__(self, kind: str, profile_root: Path, session: PrivateSession,
                 server: FixtureServer) -> None:
        if kind not in BROWSERS:
            raise ValueError(kind)
        self.kind = kind
        self._root = Path(profile_root)
        self._session = session
        self._server = server
        self._proc: subprocess.Popen | None = None
        self.profile: Path | None = None

    def open(self, url: str, incognito: bool = False) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        self.profile = Path(tempfile.mkdtemp(prefix=f"{self.kind}-", dir=self._root))
        env = dict(self._session.env)
        if self.kind == "firefox":
            (self.profile / "user.js").write_text('user_pref("browser.shell.checkDefaultBrowser", false);\nuser_pref("browser.startup.homepage_override.mstone", "ignore");\nuser_pref("browser.aboutwelcome.enabled", false);\nuser_pref("datareporting.policy.dataSubmissionEnabled", false);\nuser_pref("toolkit.telemetry.reportingpolicy.firstRun", false);\nuser_pref("browser.tabs.warnOnClose", false);\nuser_pref("browser.startup.page", 0);\nuser_pref("app.update.auto", false);\nuser_pref("extensions.autoDisableScopes", 15);' + "\n")
            argv = [BROWSERS[self.kind], "-profile", str(self.profile), "-no-remote",
                    "-new-instance", *(["-private-window"] if incognito else []), url]
        else:
            argv = [BROWSERS[self.kind], f"--user-data-dir={self.profile}", "--no-first-run",
                    "--no-default-browser-check", "--force-renderer-accessibility",
                    "--password-store=basic", "--disable-features=Translate",
                    "--window-position=0,0", "--window-size=1100,700",
                    *(["--incognito"] if incognito else []), url]
        self._proc = subprocess.Popen(argv, env=env, start_new_session=True,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._server.wait_state(lambda s: "seq" in s, 40.0)

    def close(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            try:
                os.killpg(self._proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self._proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(self._proc.pid, signal.SIGKILL)  # our own throwaway browser
        if self.profile is not None:
            shutil.rmtree(self.profile, ignore_errors=True)
            self.profile = None


# ------------------------------------------------------------------------ typing


class Typist:
    """Synthetic keyboard and mouse through XTEST, on the private server only."""

    DELAY_S = 0.008

    def __init__(self, session: PrivateSession) -> None:
        from Xlib import display

        self._display = session.display
        self._d = display.Display(session.display)
        self._spare = 0
        self._clip: Any = None

    def close(self) -> None:
        if self._clip is not None:
            self._clip.close()
        self._d.close()

    def _fake(self, kind: int, code: int) -> None:
        from Xlib.ext import xtest

        xtest.fake_input(self._d, kind, code)
        self._d.sync()

    def _tap(self, code: int, shift: bool = False) -> None:
        from Xlib import XK, X

        shift_code = self._d.keysym_to_keycode(XK.XK_Shift_L)
        if shift:
            self._fake(X.KeyPress, shift_code)
        self._fake(X.KeyPress, code)
        self._fake(X.KeyRelease, code)
        if shift:
            self._fake(X.KeyRelease, shift_code)
        time.sleep(self.DELAY_S)

    def _keycode(self, char: str) -> tuple[int, bool]:
        cp = ord(char)
        keysym = cp if 0x20 <= cp <= 0xFF else 0x01000000 + cp
        hit = list(self._d.keysym_to_keycodes(keysym))
        if hit:
            code, index = hit[0]
            return code, bool(index & 1)
        if not self._spare:  # a keycode nobody uses: map the character there on the fly
            lo, hi = self._d.display.info.min_keycode, self._d.display.info.max_keycode
            for code in range(hi, lo - 1, -1):
                if not any(self._d.get_keyboard_mapping(code, 1)[0]):
                    self._spare = code
                    break
        self._d.change_keyboard_mapping(self._spare, [(keysym,) * 4])
        self._d.sync()
        time.sleep(0.03)
        return self._spare, False

    def type(self, text: str) -> None:
        for ch in text:
            code, shift = self._keycode(ch)
            self._tap(code, shift)

    def key(self, name: str, n: int = 1) -> None:
        from Xlib import XK

        code = self._d.keysym_to_keycode(XK.string_to_keysym(name))
        assert code, name
        for _ in range(n):
            self._tap(code)

    def chord(self, *keys: str) -> None:
        from Xlib import XK, X

        codes = [self._d.keysym_to_keycode(XK.string_to_keysym(k)) for k in keys]
        assert all(codes), keys
        for c in codes:
            self._fake(X.KeyPress, c)
        for c in reversed(codes):
            self._fake(X.KeyRelease, c)
        time.sleep(self.DELAY_S)

    def click(self, x: int, y: int) -> None:
        from Xlib import X
        from Xlib.ext import xtest

        xtest.fake_input(self._d, X.MotionNotify, x=x, y=y)
        xtest.fake_input(self._d, X.ButtonPress, 1)
        xtest.fake_input(self._d, X.ButtonRelease, 1)
        self._d.sync()
        time.sleep(self.DELAY_S)

    def active_window(self) -> int:
        root = self._d.screen().root
        prop = root.get_full_property(self._d.intern_atom("_NET_ACTIVE_WINDOW"), 0)
        return int(prop.value[0]) if prop is not None and len(prop.value) else 0

    def clipboard(self) -> Any:
        """The clipboard owner, connected to the private display (never to $DISPLAY)."""
        if self._clip is None:
            from glance.writeback.clipboard_xlib import XlibClipboard

            self._clip = XlibClipboard(display_name=self._display)
        return self._clip

    def paste(self, text: str) -> None:
        """Put `text` on the clipboard and press Ctrl+V."""
        self.clipboard().set_secret_text(text, self.active_window())
        self.chord("Control_L", "v")


# ------------------------------------------------------------------------ glance under test


class GlanceUnderTest:
    """The capture process of Glance, run in the private session in its test-tap mode
    (`--print --with-text-for-tests`, honoured only with GLANCE_TEST=1). It reports exactly
    what the daemon would be sent, and with the text, which no production path does.

    The daemon-level tap (`test_state`) lives in `glance.daemon.test_hooks`; Plan 4 Task 6
    wires it into `glanced`."""

    def __init__(self, session: PrivateSession, tmp: Path) -> None:
        self._session = session
        self._tmp = Path(tmp)
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._last: Any = None
        self._count = 0
        self._lost = 0
        self.stderr = self._tmp / "capture.stderr"

    def start(self) -> GlanceUnderTest:
        from glance.protocol import SNAPSHOT, FieldSnapshot

        env = dict(self._session.env)
        env.update(GLANCE_TEST="1", GLANCE_STRICT_LOG="1")
        for name, sub in (("XDG_CONFIG_HOME", "cfg"), ("XDG_DATA_HOME", "data"),
                          ("XDG_RUNTIME_DIR", "rt"), ("XDG_STATE_HOME", "state"),
                          ("XDG_CACHE_HOME", "cache")):
            (self._tmp / sub).mkdir(mode=0o700, exist_ok=True)
            env[name] = str(self._tmp / sub)
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "glance.capture.main", "--print", "--with-text-for-tests"],
            env=env, stdout=subprocess.PIPE, stderr=self.stderr.open("w"), text=True)

        def pump() -> None:
            assert self._proc is not None and self._proc.stdout is not None
            for line in self._proc.stdout:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                with self._lock:
                    if obj.get("t") == SNAPSHOT:
                        self._last = FieldSnapshot.from_dict(obj["p"])
                        self._count += 1
                    else:
                        self._lost += 1

        threading.Thread(target=pump, daemon=True).start()
        return self

    @property
    def count(self) -> int:
        """Snapshots received so far."""
        with self._lock:
            return self._count

    def snapshot_now(self) -> Any:
        with self._lock:
            return self._last

    def last_snapshot(self, timeout_s: float = 5.0) -> Any:
        return wait_for(self.snapshot_now, timeout_s)

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.send_signal(signal.SIGTERM)
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()


# ------------------------------------------------------------------------ targets


class BrowserPage:
    """A fixture page in a browser: truth comes from the page itself."""

    has_caret_truth = True

    def __init__(self, session: PrivateSession, typist: Typist, server: FixtureServer,
                 browser: Browser, url: str, incognito: bool = False) -> None:
        self.session, self.typist, self.server = session, typist, server
        self._browser, self._url, self._incognito = browser, url, incognito

    def start(self) -> None:
        self._browser.open(self._url, incognito=self._incognito)
        self.server.cmd("focus")
        time.sleep(0.5)

    def truth(self) -> dict[str, Any]:
        return self.server.state()

    def seq(self) -> int:
        return int(self.server.state().get("seq", 0))

    def set_caret(self, frac: float) -> None:
        chars = len(list(self.truth().get("text", "")))  # code points
        self.server.cmd("setCaret", n=min(int(frac * (chars + 1)), chars))

    def stop(self) -> None:
        self._browser.close()


class GtkApp:
    """The GTK test app: truth is its buffer (SIGUSR1) and insert-mark offset (SIGUSR2)."""

    has_caret_truth = True

    def __init__(self, session: PrivateSession, typist: Typist, undo: bool = False,
                 wm_class: str | None = None, text: str = "") -> None:
        self.session, self.typist = session, typist
        self._args = [*(["--undo"] if undo else []), *(["--wm-class", wm_class] if wm_class else []),
                      *(["--text", text] if text else [])]
        self._proc: subprocess.Popen | None = None
        self._lines: queue.Queue = queue.Queue()

    def start(self) -> None:
        self._proc = subprocess.Popen(
            [sys.executable, str(GTK_APP), *self._args], env=self.session.env,
            stdout=subprocess.PIPE,
            text=True)
        assert self._proc.stdout is not None
        threading.Thread(target=lambda: [self._lines.put(line.rstrip("\n"))
                                         for line in self._proc.stdout], daemon=True).start()
        wait_for(lambda: self._lines.get(timeout=10) == "READY", 20.0)
        time.sleep(0.5)

    def truth(self) -> dict[str, Any]:
        assert self._proc is not None
        while not self._lines.empty():
            self._lines.get_nowait()
        self._proc.send_signal(signal.SIGUSR1)
        text = json.loads(self._lines.get(timeout=5))
        self._proc.send_signal(signal.SIGUSR2)
        caret = int(self._lines.get(timeout=5).split()[1])
        return {"text": text, "caret": caret}

    def seq(self) -> int:
        return 0

    def set_caret(self, frac: float) -> None:
        chars = len(list(self.truth()["text"]))  # one line: Home, then Right n times
        self.typist.chord("Control_L", "Home")
        self.typist.key("Right", min(int(frac * (chars + 1)), chars))

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.send_signal(signal.SIGTERM)
            self._proc.wait(timeout=10)


# ------------------------------------------------------------------------ the matrix


@dataclass
class MatrixResult:
    total: int
    exact: int
    mismatches: list[dict[str, Any]] = field(default_factory=list)
    # Proof the edits really happened (the editor's own text, not Glance's):
    type_ops: int = 0
    type_changed: int = 0
    paste_ops: int = 0
    paste_landed: int = 0
    final_text: str = ""


def _perform(op: Op, target: Any) -> None:
    t = target.typist
    if op.kind == "type":
        t.type(op.text)
    elif op.kind == "backspace":
        t.key("BackSpace", op.n)
    elif op.kind == "arrow":
        t.key(op.text, op.n)
    elif op.kind == "home_end":
        t.key(op.text)
    elif op.kind == "set_caret":
        target.set_caret(op.frac)
    elif op.kind == "paste":
        t.paste(op.text)
    elif op.kind == "select_type":
        for _ in range(op.n):
            t.chord("Shift_L", "Left")
        t.type(op.text)


def settle(target: Any, glance: GlanceUnderTest, quiet_s: float = QUIET_S,
           timeout_s: float = 5.0) -> None:
    """Return once the page's seq and Glance's seq have both been still for `quiet_s`."""
    last = (target.seq(), glance.count)
    since = time.monotonic()
    deadline = since + timeout_s
    while time.monotonic() - since < quiet_s and time.monotonic() < deadline:
        time.sleep(0.01)
        now = (target.seq(), glance.count)
        if now != last:
            last, since = now, time.monotonic()


def run_capture_matrix(target: Any, glance: GlanceUnderTest, n: int = 200,
                       seed: int = 1) -> MatrixResult:
    """Run `edit_script(seed, n)` against `target`; after every op compare what Glance
    captured with what the editor itself says."""
    result = MatrixResult(total=0, exact=0)
    for index, op in enumerate(edit_script(seed, n)):
        before = target.truth().get("text", "")
        _perform(op, target)
        settle(target, glance)
        truth, snap = target.truth(), glance.snapshot_now()
        result.total += 1
        if op.kind == "type":
            result.type_ops += 1
            result.type_changed += truth.get("text", "") != before
        elif op.kind == "paste":
            result.paste_ops += 1
            result.paste_landed += op.text in truth.get("text", "")
        result.final_text = truth.get("text", "")
        ok = snap is not None and norm(snap.text) == norm(truth.get("text", ""))
        if ok and target.has_caret_truth:
            ok = snap.caret == truth.get("caret")
        if ok:
            result.exact += 1
        else:
            result.mismatches.append({
                "op": index, "kind": op.kind,
                "snapshot_len": None if snap is None else len(snap.text),
                "truth_len": len(truth.get("text", "")),
                "snapshot_caret": None if snap is None else snap.caret,
                "truth_caret": truth.get("caret"),
            })
    return result


# ------------------------------------------------------------------------ the daemon

HARPER = Path.home() / ".local/share/glance/bin/harper-ls"
FAKE_CLI = REPO / "tests" / "fake_cli"


class GlancedUnderTest:
    """The shipped `glanced` (with its own capture, harper-ls and backend) in the private
    session, with temporary XDG directories. `backend`:

    - "fake"     the in-process test backend (no process, nothing on PATH is read)
    - "fake_cli" the real backend code path (shield, argv/stdin) against `tests/fake_cli/claude`,
                 which appends everything it is sent to `stdin_log` (FAKE_STDIN_LOG)
    """

    def __init__(self, session: PrivateSession, root: Path, backend: str = "fake",
                 fake_mode: str = "analyze") -> None:
        self.root = Path(root)
        self.dirs = {n: self.root / n for n in ("rt", "cfg", "data", "state", "cache")}
        for d in self.dirs.values():
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.dirs["data"] / "glance" / "bin").mkdir(parents=True, exist_ok=True)
        if HARPER.exists():
            (self.dirs["data"] / "glance" / "bin" / "harper-ls").symlink_to(HARPER)
        (self.root / "spy").mkdir(exist_ok=True)  # outside every directory Glance writes to
        self.stdin_log = self.root / "spy" / "fake-cli.log"
        self.env = {**session.env, "XDG_RUNTIME_DIR": str(self.dirs["rt"]),
                    "XDG_CONFIG_HOME": str(self.dirs["cfg"]),
                    "XDG_DATA_HOME": str(self.dirs["data"]),
                    "XDG_STATE_HOME": str(self.dirs["state"]),
                    "XDG_CACHE_HOME": str(self.dirs["cache"]),
                    "GLANCE_STRICT_LOG": "1", "GLANCE_TEST": "1"}
        if backend == "fake":
            self.env["GLANCE_FAKE_BACKEND"] = "1"
        elif backend == "fake_cli":
            self.stdin_log.touch()
            self.env.update(PATH=f"{FAKE_CLI}:{os.environ['PATH']}",
                            FAKE_STDIN_LOG=str(self.stdin_log), FAKE_MODE=fake_mode,
                            FAKE_AUTH="in")
            (self.dirs["cfg"] / "glance").mkdir(exist_ok=True)
            (self.dirs["cfg"] / "glance" / "config.toml").write_text(
                '[backend]\nchoice = "claude"\nwarm = false\n')
        else:
            raise ValueError(backend)
        self.stderr_path = self.root / "glanced.stderr"
        self.sock = self.dirs["rt"] / "glance" / "ctl.sock"
        self.proc: subprocess.Popen | None = None

    def start(self) -> GlancedUnderTest:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "glance.daemon.main"],
            env={**self.env, "NO_AT_BRIDGE": "1"}, stderr=self.stderr_path.open("w"),
            stdout=subprocess.DEVNULL)
        wait_for(lambda: self.status().get("capture_connected"), 40.0)
        return self

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

    def snapshot_text(self) -> str | None:
        snap = self.state().get("snapshot")
        return None if snap is None else snap["text"]

    def cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "glance.cli", *args], env=self.env,
                              capture_output=True, text=True, timeout=60, check=False)

    def descendants(self) -> list[int]:
        """pids of every descendant of glanced (from /proc)."""
        assert self.proc is not None
        parents: dict[int, int] = {}
        for entry in Path("/proc").iterdir():
            if entry.name.isdigit():
                try:
                    parents[int(entry.name)] = int(
                        (entry / "stat").read_text().rsplit(")", 1)[1].split()[1])
                except (OSError, ValueError, IndexError):
                    continue
        found: list[int] = []
        todo = [self.proc.pid]
        while todo:
            cur = todo.pop()
            kids = [p for p, parent in parents.items() if parent == cur]
            found += kids
            todo += kids
        return found

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()  # a process of this private session


# ------------------------------------------------------------------------ clipboard probe


class ClipProbe:
    """Owns CLIPBOARD on the private display with a known text, and reads whatever the
    clipboard holds now. Two connections: an owner thread that answers requests and a reader."""

    def __init__(self, display_name: str) -> None:
        from Xlib import X, display

        self._X = X
        self._owner_d = display.Display(display_name)
        self._reader_d = display.Display(display_name)
        self._atoms = {n: self._owner_d.intern_atom(n)
                       for n in ("CLIPBOARD", "TARGETS", "UTF8_STRING", "STRING", "ATOM")}
        scr = self._owner_d.screen()
        self._win = scr.root.create_window(0, 0, 1, 1, 0, scr.root_depth)
        rscr = self._reader_d.screen()
        self._rwin = rscr.root.create_window(0, 0, 1, 1, 0, rscr.root_depth)
        self._prop = self._reader_d.intern_atom("GLANCE_PROBE")
        self._text = ""
        self._stop = False
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def own(self, text: str) -> None:
        with self._lock:
            self._text = text
            self._win.set_selection_owner(self._atoms["CLIPBOARD"], self._X.CurrentTime)
            self._owner_d.sync()

    def _serve(self) -> None:
        from Xlib.protocol import event

        X, a = self._X, self._atoms
        while not self._stop:
            try:
                with self._lock:
                    while self._owner_d.pending_events():
                        ev = self._owner_d.next_event()
                        if ev.type != X.SelectionRequest:
                            continue
                        prop, ok = ev.property or ev.target, True
                        if ev.target == a["TARGETS"]:
                            ev.requestor.change_property(
                                prop, a["ATOM"], 32, [a["TARGETS"], a["UTF8_STRING"], a["STRING"]])
                        elif ev.target in (a["UTF8_STRING"], a["STRING"]):
                            ev.requestor.change_property(prop, ev.target, 8,
                                                         self._text.encode("utf-8"))
                        else:
                            ok = False
                        ev.requestor.send_event(event.SelectionNotify(
                            time=ev.time, requestor=ev.requestor, selection=ev.selection,
                            target=ev.target, property=prop if ok else X.NONE), onerror=None)
                        self._owner_d.flush()
            except Exception:  # noqa: BLE001 - a closing display ends the thread
                return
            time.sleep(0.005)

    def read(self, timeout_s: float = 2.0) -> str | None:
        """The clipboard text now (None: empty or nobody answered)."""
        X, d = self._X, self._reader_d
        self._rwin.delete_property(self._prop)
        self._rwin.convert_selection(self._atoms["CLIPBOARD"], self._atoms["UTF8_STRING"],
                                     self._prop, X.CurrentTime)
        d.flush()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            while d.pending_events():
                ev = d.next_event()
                if ev.type == X.SelectionNotify:
                    if ev.property == X.NONE:
                        return None
                    got = self._rwin.get_full_property(self._prop, X.AnyPropertyType)
                    if got is None:
                        return None
                    v = got.value
                    return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)
            time.sleep(0.005)
        return None

    def close(self) -> None:
        self._stop = True
        self._thread.join(timeout=2)
        self._owner_d.close()
        self._reader_d.close()


# ------------------------------------------------------------------------ write-back matrix

SEED_TEXT = ("Teh quick brown fox jumps over the lazy dog. We will meet tomorrow morning to "
             "review the draft report. Please send the updated budget before the deadline.")
_REPLY_WORDS = [w.rstrip(",.!") for w in _BASE]


@dataclass
class WbResult:
    total: int = 0
    verified: int = 0
    reverted: int = 0
    refused: int = 0
    stale: int = 0
    failed: int = 0
    unverified: int = 0
    no_result: int = 0
    not_synced: int = 0
    failed_dirty: int = 0
    silent_corruptions: list[dict[str, Any]] = field(default_factory=list)
    undo_checked: int = 0
    undo_ok: int = 0
    undo_failures: list[dict[str, Any]] = field(default_factory=list)
    clipboard_violations: int = 0
    details: dict[str, int] = field(default_factory=dict)  # `status: detail` -> count
    notes: list[str] = field(default_factory=list)


def _words(rng: random.Random, lo: int, hi: int) -> str:
    out = ""
    while len(out) < lo:
        out += rng.choice(_REPLY_WORDS) + " "
    return out[:hi].rstrip()


def wb_replacement(rng: random.Random) -> str:
    r = rng.random()
    if r < 0.40:
        return _words(rng, 1, 20)
    if r < 0.80:
        return _words(rng, 81, 400)
    if r < 0.90:
        return ""
    return "é — 😀 " + _words(rng, 3, 14)


def wb_range(rng: random.Random, n: int) -> tuple[int, int]:
    r = rng.random()
    if r < 0.10:
        i = rng.randint(0, n)
        return i, i
    if r < 0.20:
        return 0, n
    start = rng.randint(0, max(n - 1, 0))
    return start, min(n, start + rng.randint(1, 60))


def _reset_page(target: Any, typist: Typist) -> None:
    if isinstance(target, GtkApp):
        typist.chord("Control_L", "a")
        typist.paste(SEED_TEXT)
    else:
        target.server.cmd("setText", text=SEED_TEXT)
    wait_for(lambda: norm(target.truth().get("text", "")) == norm(SEED_TEXT), 5.0)
    time.sleep(0.3)


def _page_quiet(target: Any, quiet_s: float = 0.2, timeout_s: float = 4.0) -> None:
    last, since = (target.seq(), target.truth().get("text")), time.monotonic()
    deadline = since + timeout_s
    while time.monotonic() - since < quiet_s and time.monotonic() < deadline:
        time.sleep(0.02)
        now = (target.seq(), target.truth().get("text"))
        if now != last:
            last, since = now, time.monotonic()


def _daemon_has(glance: GlancedUnderTest, text: str, timeout_s: float = 4.0) -> bool:
    try:
        return bool(wait_for(lambda: glance.snapshot_text() is not None
                             and norm(glance.snapshot_text()) == norm(text), timeout_s, 0.05))
    except TimeoutError:
        return False


def run_writeback_matrix(target: Any, glance: GlancedUnderTest, typist: Typist,
                         probe: ClipProbe, n: int = 500, seed: int = 2,
                         reset_every: int = 25) -> WbResult:
    """`n` scripted applies through the daemon's verified write-back path (ctl `test_apply`),
    each judged against the editor's own text before and after."""
    from glance.grammar.issues import apply_edit
    from glance.protocol import Edit

    rng = random.Random(seed)
    res = WbResult()
    emoji_in_page = False
    for i in range(n):
        # An emoji is a protected entity: the engine rightly refuses a later edit that cuts
        # through one, so the page is reset after an emoji lands (it is the seed text again).
        if i % reset_every == 0 or emoji_in_page:
            _reset_page(target, typist)
            emoji_in_page = False
        sentinel = f"CLIP-SENTINEL-{i}"
        probe.own(sentinel)
        before = target.truth().get("text", "")
        # The daemon must hold the very text the page holds, or the apply is legitimately stale.
        if not _daemon_has(glance, before):
            res.not_synced += 1
            res.notes.append(f"{i}: daemon text differs from the page; skipped")
            _reset_page(target, typist)
            continue
        have = glance.snapshot_text() or before
        start, end = wb_range(rng, len(have))
        replacement = wb_replacement(rng)
        expected = apply_edit(have, Edit(start, end, replacement))
        res.total += 1
        apply_id = ""
        deadline = time.monotonic() + 6.0
        while not apply_id and time.monotonic() < deadline:
            got = glance.request("test_apply", {"start": start, "end": end,
                                                "replacement": replacement, "method": "auto"})
            apply_id = got.get("apply_id", "")
            if not apply_id:
                time.sleep(0.2)
        result: dict | None = None
        if apply_id:
            try:
                result = wait_for(lambda a=apply_id: glance.request(
                    "test_apply_result", {"apply_id": a}).get("result"), 20.0, 0.05)
            except TimeoutError:
                result = None
        status = result["status"] if result else "no_result"
        emoji_in_page = "\U0001f600" in replacement
        _page_quiet(target)
        after = target.truth().get("text", "")
        setattr(res, status, getattr(res, status) + 1)
        if status != "verified":
            key = f"{status}: {(result or {}).get('detail', '')}"
            res.details[key] = res.details.get(key, 0) + 1
        record = {"i": i, "status": status, "range": (start, end),
                  "replacement": replacement[:40], "detail": (result or {}).get("detail", ""),
                  "before_len": len(before), "after_len": len(after)}
        if status == "verified":
            if norm(after) != norm(expected):
                res.silent_corruptions.append({**record, "why": "verified but text differs"})
        elif norm(after) != norm(before):
            res.failed_dirty += status == "failed"
            res.silent_corruptions.append({**record, "why": f"{status} but text changed"})
        cb = probe.read()
        if cb != sentinel:
            res.clipboard_violations += 1
            res.notes.append(f"{i}: clipboard {cb!r} != {sentinel!r} after {status}")
        if status == "verified" and res.verified % 10 == 0 and norm(after) != norm(before):
            res.undo_checked += 1
            typist.chord("Control_L", "z")
            _page_quiet(target)
            undone = target.truth().get("text", "")
            if norm(undone) == norm(before):
                res.undo_ok += 1
            else:
                res.undo_failures.append({**record, "undone_len": len(undone)})
                _reset_page(target, typist)
    return res
