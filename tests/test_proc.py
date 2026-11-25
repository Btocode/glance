import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from glance.backends.base import BackendError
from glance.backends.proc import ProcRunner, empty_dir

FAKE = str(Path(__file__).parent / "fake_cli" / "claude")


@pytest.fixture(autouse=True)
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))


def _pgid_alive(marker: str) -> bool:
    out = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True, check=False).stdout
    return bool(out.strip())


def test_runner_timeout_kills_process_group():
    marker = "sleep 31.4159"
    t0 = time.monotonic()
    with pytest.raises(BackendError) as ei:
        ProcRunner().run(["sh", "-c", f"{marker} & {marker}"], "", 0.3)
    assert ei.value.kind == "timeout"
    assert time.monotonic() - t0 < 3
    time.sleep(1.5)
    assert not _pgid_alive(marker)


def test_runner_cancel_from_other_thread():
    runner = ProcRunner()
    threading.Timer(0.3, runner.cancel).start()
    t0 = time.monotonic()
    with pytest.raises(BackendError) as ei:
        runner.run(["sleep", "30"], "", 10)
    assert ei.value.kind == "cancelled"
    assert time.monotonic() - t0 < 3


def test_runner_cancel_stream():
    runner = ProcRunner()
    threading.Timer(0.3, runner.cancel).start()
    with pytest.raises(BackendError) as ei:
        list(runner.stream(["sleep", "30"], "", 10))
    assert ei.value.kind == "cancelled"


def test_runner_stream_timeout():
    with pytest.raises(BackendError) as ei:
        list(ProcRunner().stream(["sleep", "30"], "", 0.3))
    assert ei.value.kind == "timeout"


def test_runner_missing_binary_is_unavailable():
    with pytest.raises(BackendError) as ei:
        ProcRunner().run(["/nonexistent/glance-bin"], "", 1)
    assert ei.value.kind == "unavailable"
    with pytest.raises(BackendError) as ei:
        list(ProcRunner().stream(["/nonexistent/glance-bin"], "", 1))
    assert ei.value.kind == "unavailable"


def test_runner_run_and_stream_roundtrip():
    r = ProcRunner().run(["cat"], "hello\n", 2)
    assert (r.stdout, r.returncode) == ("hello\n", 0)
    assert list(ProcRunner().stream(["cat"], "a\nb\n", 2)) == ["a\n", "b\n"]


def test_runner_cwd_is_private_empty_dir(tmp_path):
    log = tmp_path / "log.jsonl"
    runner = ProcRunner(env={"FAKE_LOG": str(log), "FAKE_MODE": "analyze"})
    runner.run([sys.executable, FAKE, "-p"], "x", 5)
    import json

    rec = json.loads(log.read_text().splitlines()[0])
    d = empty_dir()
    assert rec["cwd"] == str(d)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert os.listdir(d) == []


def test_child_runs_in_own_session():
    r = ProcRunner().run(["sh", "-c", "ps -o pgid= -p $$; echo $$"], "", 2)
    pgid, pid = (int(x) for x in r.stdout.split())
    assert pgid == pid != os.getpgid(0)


def test_idle_proc_feed_and_kill():
    idle = ProcRunner().spawn_idle(["cat"])
    assert idle.alive
    assert list(idle.feed("one\ntwo\n")) == ["one\n", "two\n"]
    idle2 = ProcRunner().spawn_idle(["sleep", "30"])
    idle2.kill()
    assert not idle2.alive
