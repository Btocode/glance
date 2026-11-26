"""glanced leaves fast: a slow step or a busy worker never holds the exit."""
from __future__ import annotations

import subprocess
import sys
import threading
import time

from glance.daemon import teardown as td
from glance.daemon.main import hide_everything, stop_all, stop_backends
from glance.daemon.supervisor import ChildSupervisor
from tests.fakes import FakeClock


def test_steps_run_in_order_and_a_failing_one_does_not_stop_the_rest(capsys):
    seen: list[str] = []
    exits: list[int] = []

    def boom() -> None:
        seen.append("b")
        raise RuntimeError("x")

    t = td.Teardown(deadline_s=5, exit=exits.append)
    t.run([("a", lambda: seen.append("a")), ("b", boom), ("c", lambda: seen.append("c"))])
    t.finish([])
    assert seen == ["a", "b", "c"] and exits == [] and not t.forced
    err = capsys.readouterr().err
    assert "teardown_step_failed step=b" in err
    assert "glanced_stopped ms=" in err  # how long leaving took, and its slowest step


def test_a_backend_stop_that_hangs_ends_in_a_forced_exit(capsys):
    exited = threading.Event()
    release = threading.Event()
    t = td.Teardown(deadline_s=0.3, exit=lambda code: exited.set())
    start = time.monotonic()
    runner = threading.Thread(
        target=t.run, args=([("overlay", lambda: None), ("backends", release.wait)],),
        daemon=True)
    runner.start()
    assert exited.wait(2)
    assert time.monotonic() - start < 1.0
    release.set()
    runner.join(2)
    assert t.forced
    assert "glanced_forced_exit step=backends" in capsys.readouterr().err


def test_a_busy_worker_thread_does_not_hold_the_exit(capsys):
    # an AI call blocked in a pool worker: the interpreter would join it at exit
    stop = threading.Event()
    worker = threading.Thread(target=stop.wait, args=(30,), daemon=False)
    worker.start()
    exits: list[int] = []
    try:
        t = td.Teardown(deadline_s=0.3, exit=exits.append)
        t.run([])
        start = time.monotonic()
        t.finish([worker])
        assert time.monotonic() - start < 1.0
        assert exits == [0] and t.forced
        assert "glanced_forced_exit step=threads" in capsys.readouterr().err
    finally:
        stop.set()
        worker.join()


def test_a_real_process_with_a_stuck_worker_exits_within_the_deadline(tmp_path):
    """End to end: a non-daemon thread blocked for a minute would hold a plain sys.exit."""
    script = tmp_path / "stuck.py"
    script.write_text(
        "import threading, time\n"
        "from glance.daemon.teardown import Teardown\n"
        "threading.Thread(target=time.sleep, args=(60,)).start()\n"
        "t = Teardown(deadline_s=0.5)\n"
        "t.run([('backends', lambda: time.sleep(0.1))])\n"
        "t.finish()\n")
    start = time.monotonic()
    done = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                          timeout=20, check=False)
    assert time.monotonic() - start < 5
    assert done.returncode == 0
    assert "glanced_forced_exit step=threads" in done.stderr


class _Stubborn:
    """A child that ignores SIGTERM (records what it was sent)."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.dead = False

    def poll(self):
        return 0 if self.dead else None

    def terminate(self) -> None:
        self.sent.append("TERM")

    def wait(self, timeout=None):
        if not self.dead:
            raise subprocess.TimeoutExpired("child", timeout)
        return 0

    def kill(self) -> None:
        self.sent.append("KILL")
        self.dead = True


def test_children_get_sigterm_then_sigkill_and_stop_together(monkeypatch):
    monkeypatch.setattr("glance.daemon.supervisor.STOP_WAIT_S", 0.01)

    kids = [_Stubborn(), _Stubborn()]
    sups = []
    for kid in kids:
        sup = ChildSupervisor("kid", lambda k=kid: k, FakeClock(), on_state=lambda s: None)
        sup.start()
        sups.append(sup)
    stop_all([s.stop for s in sups])
    assert [k.sent for k in kids] == [["TERM", "KILL"], ["TERM", "KILL"]]


def test_real_child_ignoring_sigterm_is_killed_within_about_a_second():
    code = ("import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            "print('up', flush=True); time.sleep(60)")
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "up"
    sup = ChildSupervisor("kid", lambda: child, FakeClock(), on_state=lambda s: None)
    sup.start()
    start = time.monotonic()
    sup.stop()
    assert child.poll() is not None
    assert time.monotonic() - start < 2.0


def test_backends_stop_at_the_same_time():
    class Slow:
        def __init__(self) -> None:
            self.stopped = False

        def stop(self) -> None:
            time.sleep(0.3)
            self.stopped = True

    class Closes:
        closed = False

        def close(self) -> None:
            self.closed = True

    backends = [Slow(), Slow(), Slow(), Closes()]
    start = time.monotonic()
    stop_backends(backends)
    assert time.monotonic() - start < 0.8
    assert all(b.stopped for b in backends[:3]) and backends[3].closed


def test_overlays_go_first_even_when_one_hide_fails():
    calls: list[str] = []

    class Overlay:
        def hide_strip(self) -> None:
            calls.append("strip")
            raise RuntimeError("gone")

        def hide_panel(self) -> None:
            calls.append("panel")

    class Ghost:
        def hide(self) -> None:
            calls.append("ghost")

    class Shell:
        available = True

        def call(self, method, payload=None, timeout_ms=0):
            calls.append(f"{method}{payload}{timeout_ms}")

    hide_everything(Overlay(), Ghost(), Shell())
    assert calls == ["strip", "panel", "ghost", "Render{}300"]


def test_idle_pool_workers_do_not_cost_the_deadline(tmp_path):
    """Pools nobody shut down (an idle worker waits forever) end at once, not by force."""
    script = tmp_path / "idle.py"
    script.write_text(
        "from concurrent.futures import ThreadPoolExecutor\n"
        "from glance.daemon.teardown import Teardown\n"
        "pool = ThreadPoolExecutor(2)\n"
        "pool.submit(lambda: None).result()\n"
        "t = Teardown(deadline_s=2.5)\n"
        "t.run([])\n"
        "t.finish()\n"
        "print('forced' if t.forced else 'clean')\n")
    start = time.monotonic()
    done = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                          timeout=20, check=False)
    assert done.stdout.strip() == "clean", done.stderr
    assert time.monotonic() - start < 2.0
