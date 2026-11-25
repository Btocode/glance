"""Resource gates (spec §8.3): idle CPU, CPU while typing, RSS, on a private Xvfb against the
real glanced, glance-capture and harper-ls with the fake backend. The tests share one daemon
and run in file order (about 3 minutes).

    .venv/bin/pytest -m xvfb tests/acceptance/test_resources.py -v -s
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from tests.acceptance.harness import Typist
from tests.acceptance.perf import (
    HARPER,
    TICKS,
    TimedApp,
    TimedGlanced,
    _ticks,
    comm,
    proc_cpu,
    rss_mb,
    type_paced,
    wait_for,
)

pytestmark = [
    pytest.mark.xvfb,
    pytest.mark.skipif(not HARPER.exists(), reason="harper-ls is not installed"),
]


@pytest.fixture(scope="module")
def rig(session, tmp_path_factory):
    typist = Typist(session)
    with TimedApp(session, timing=False) as app, \
            TimedGlanced(session, tmp_path_factory.mktemp("g")) as g:
        assert wait_for(lambda: g.status().get("capture_connected") and g.status().get("ai")
                        == "ok", 40.0), g.log()[-2000:]
        time.sleep(3.0)
        yield SimpleNamespace(app=app, g=g, typist=typist, session=session)
    typist.close()


def names(pids: list[int]) -> list[str]:
    return [f"{comm(p)}({p})" for p in pids]


def test_idle_cpu(rig):
    pids = rig.g.pids()
    assert len(pids) >= 3, names(pids)  # glanced, glance-capture, harper-ls
    cpu = proc_cpu(pids, 60.0)
    print("\nidle CPU over 60 s, % of one core:", round(cpu, 3), names(pids))
    assert cpu < 0.5


def test_typing_cpu(rig):
    pids = rig.g.pids()
    text = "I will recieve the report from teh team today and then reply to them soon. " * 6
    before = {p: _ticks(p) for p in pids}
    cpu = proc_cpu(pids, 60.0, during=lambda: type_paced(rig.typist, text[:360], 1 / 6))
    each = {n: round((_ticks(p) - before[p]) / TICKS / 60.0 * 100, 2)
            for n, p in zip(names(pids), pids)}
    print("\ntyping CPU over 60 s at 6 keys/s, % of one core:", round(cpu, 3), each)
    assert cpu < 3.0


def test_rss(rig):
    """Ruling: harper-ls is a fixed-cost engine (its dictionary alone is ~150 MB anonymous memory,
    unchanged by disabling linters or malloc tuning), so like the AI backends' worker processes it is
    outside the 250 MB bar, which covers glanced + glance-capture; harper-ls gets its own cap."""
    pids = rig.g.pids()
    per = {f"{comm(p)}({p})": round(rss_mb([p]), 1) for p in pids}
    harper = [p for p in pids if comm(p) == "harper-ls"]
    own = [p for p in pids if p not in harper]
    total, h = rss_mb(own), rss_mb(harper)
    workers = [p for p in rig.g.pids(workers=True) if p not in pids]
    print("\nRSS MB:", per, "| glanced+capture", round(total, 1), "| harper-ls", round(h, 1),
          "| backend workers:", {f"{comm(p)}({p})": round(rss_mb([p]), 1) for p in workers})
    assert total < 250
    assert h < 200
