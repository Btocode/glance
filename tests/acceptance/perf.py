"""Measurement helpers for the latency and resource gates: a glanced with timing probes
(`timed_glanced`), the GTK test app in timing mode, `/proc` CPU and RSS, percentiles.

Everything runs on the acceptance `session` (private Xvfb, own D-Bus and AT-SPI bus).
"""
from __future__ import annotations

import json
import os
import queue
import re
import signal
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from tests.acceptance.harness import GTK_APP, REPO

HARPER = Path.home() / ".local/share/glance/bin/harper-ls"
TICKS = os.sysconf("SC_CLK_TCK")
_NUM = re.compile(r"(\w+)=(-?[\d.]+)")


# ------------------------------------------------------------------ statistics


def pct(values: list[float], p: float) -> float:
    """The p-th percentile (nearest rank) of `values`."""
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(p / 100 * len(ordered) + 0.5) - 1))]


def summary(values: list[float]) -> str:
    if not values:
        return "n=0"
    return (f"n={len(values)} p50={statistics.median(values):.2f} p95={pct(values, 95):.2f} "
            f"p99={pct(values, 99):.2f} max={max(values):.2f}")


# ------------------------------------------------------------------ /proc


def children(pid: int) -> list[int]:
    """Every descendant of `pid`."""
    out, todo = [], [pid]
    while todo:
        cur = todo.pop()
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text()
                ppid = int(stat[stat.rindex(")") + 2:].split()[1])
            except (OSError, ValueError):
                continue
            if ppid == cur:
                out.append(int(entry.name))
                todo.append(int(entry.name))
    return out


def comm(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return ""


def _ticks(pid: int) -> int:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return 0  # gone
    fields = stat[stat.rindex(")") + 2:].split()
    return int(fields[11]) + int(fields[12])  # utime + stime


def proc_cpu(pids: list[int], seconds: float, during: Any = None) -> float:
    """CPU of `pids` over `seconds`, in percent of one core (`during()` runs meanwhile,
    otherwise this sleeps)."""
    before = {p: _ticks(p) for p in pids}
    t0 = time.monotonic()
    if during is None:
        time.sleep(seconds)
    else:
        during()
    wall = time.monotonic() - t0
    used = sum(max(0, _ticks(p) - before[p]) for p in pids)
    return used / TICKS / wall * 100


def rss_mb(pids: list[int]) -> float:
    total_kb = 0
    for pid in pids:
        try:
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    total_kb += int(line.split()[1])
        except OSError:
            continue
    return total_kb / 1024


# ------------------------------------------------------------------ the daemon


def wait_for(pred: Any, timeout_s: float, step_s: float = 0.03) -> Any:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            value = pred()
        except (queue.Empty, KeyError, TypeError):
            value = None
        if value or time.monotonic() > deadline:
            return value
        time.sleep(step_s)


class TimedGlanced:
    """glanced (through `timed_glanced`) with temp XDG directories in the private session.

        `config`   extra TOML for config.toml
        `fake`     the in-process fake backend (GLANCE_FAKE_BACKEND=1), else the real PATH
        `env`      extra environment, e.g. GLANCE_FAKE_COMPLETE=none
    """

    def __init__(self, session: Any, root: Path, config: str = "", fake: bool = True,
                 env: dict[str, str] | None = None) -> None:
        self.dirs = {n: root / n for n in ("rt", "cfg", "data")}
        for d in self.dirs.values():
            d.mkdir(mode=0o700, parents=True)
        (self.dirs["data"] / "glance" / "bin").mkdir(parents=True)
        (self.dirs["data"] / "glance" / "bin" / "harper-ls").symlink_to(HARPER)
        if config:
            (self.dirs["cfg"] / "glance").mkdir()
            (self.dirs["cfg"] / "glance" / "config.toml").write_text(config)
        self.env = {**session.env, "XDG_RUNTIME_DIR": str(self.dirs["rt"]),
                    "XDG_CONFIG_HOME": str(self.dirs["cfg"]),
                    "XDG_DATA_HOME": str(self.dirs["data"]), "GLANCE_STRICT_LOG": "1",
                    "GLANCE_TEST": "1", "PYTHONPATH": f"{REPO}{os.pathsep}{REPO / 'src'}", **(env or {})}
        if fake:
            self.env["GLANCE_FAKE_BACKEND"] = "1"
        self.log_path = root / "glanced.stderr"
        self.sock = self.dirs["rt"] / "glance" / "ctl.sock"
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "tests.acceptance.timed_glanced"], cwd=REPO,
            env={**self.env, "NO_AT_BRIDGE": "1"},
            stderr=self.log_path.open("w"), stdout=subprocess.DEVNULL)

    def request(self, msg_type: str, payload: dict | None = None, timeout_s: float = 5.0) -> dict:
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

    def log(self) -> str:
        return self.log_path.read_text()

    def timings(self, event: str, field: str = "ms", since: int = 0) -> list[float]:
        """`field` of every `event` line in the daemon's stderr, from line index `since`."""
        out = []
        for line in self.log().splitlines()[since:]:
            if line.startswith(event + " "):
                for key, val in _NUM.findall(line):
                    if key == field:
                        out.append(float(val))
        return out

    def count(self, event: str) -> int:
        return sum(1 for line in self.log().splitlines() if line.startswith(event + " "))

    def wait_count(self, event: str, above: int, timeout_s: float) -> bool:
        """Wait until there are more than `above` lines of `event`."""
        return bool(wait_for(lambda: self.count(event) > above, timeout_s))

    def pids(self, workers: bool = False) -> list[int]:
        """glanced and its children; the AI backends' worker processes only when asked."""
        kids = children(self.proc.pid)
        if not workers:
            kids = [p for p in kids if comm(p) not in ("claude", "node", "codex", "gemini",
                                                       "opencode")]
        return [self.proc.pid, *kids]

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()  # a process of this private session

    def __enter__(self) -> TimedGlanced:  # noqa: PYI034
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


class TimedApp:
    """gtk_app.py with `--timing`: `deltas` collects its per-key `DELTA <ns>` lines."""

    def __init__(self, session: Any, text: str = "", accepts_tab: bool = False,
                 timing: bool = True, undo: bool = False) -> None:
        argv = [sys.executable, str(GTK_APP), "--text", text]
        argv += ["--timing"] * timing + ["--accepts-tab"] * accepts_tab + ["--undo"] * undo
        self.proc = subprocess.Popen(argv, env=session.env, stdout=subprocess.PIPE, text=True)
        self.lines: queue.Queue = queue.Queue()
        self.deltas: list[float] = []  # ms
        self._ready = threading.Event()
        threading.Thread(target=self._pump, daemon=True).start()
        assert self._ready.wait(20), "gtk_app not ready"
        time.sleep(0.5)

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            line = raw.rstrip("\n")
            if line == "READY":
                self._ready.set()
            elif line.startswith("DELTA "):
                self.deltas.append(int(line.split()[1]) / 1e6)
            else:
                self.lines.put(line)

    def buffer(self) -> str:
        while not self.lines.empty():
            self.lines.get_nowait()
        self.proc.send_signal(signal.SIGUSR1)
        return json.loads(self.lines.get(timeout=5))

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            self.proc.wait(timeout=10)

    def __enter__(self) -> TimedApp:  # noqa: PYI034
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


def windows_of(session: Any, pid: int) -> list[int]:
    """Mapped top-level windows owned by `pid` on the private display (the chip, the ghost)."""
    from Xlib import X, display, error

    d = display.Display(session.display)
    try:
        out = []
        for w in d.screen().root.query_tree().children:
            try:
                owner = w.get_full_property(d.intern_atom("_NET_WM_PID"), X.AnyPropertyType)
                if owner and int(owner.value[0]) == pid \
                        and w.get_attributes().map_state == X.IsViewable:
                    out.append(w.id)
            except error.XError:
                continue
        return out
    finally:
        d.close()


def type_paced(typist: Any, keys: str, interval_s: float) -> None:
    """Type `keys`, one every `interval_s` on a fixed schedule (no drift)."""
    t = time.monotonic()
    old, typist.DELAY_S = typist.DELAY_S, 0.0
    try:
        for ch in keys:
            code, shift = typist._keycode(ch)
            typist._tap(code, shift)
            t += interval_s
            time.sleep(max(0.0, t - time.monotonic()))
    finally:
        typist.DELAY_S = old
