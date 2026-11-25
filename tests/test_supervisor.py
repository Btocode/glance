import subprocess

from glance.daemon.supervisor import ChildSupervisor
from tests.fakes import FakeClock


class FakeChild:
    def __init__(self, alive: bool) -> None:
        self.alive = alive
        self.terminated = self.killed = False
        self.stubborn = False

    def poll(self):
        return None if self.alive else 1

    def terminate(self) -> None:
        self.terminated = True
        if not self.stubborn:
            self.alive = False

    def wait(self, timeout=None):
        if self.alive:
            raise subprocess.TimeoutExpired("child", timeout)
        return 0

    def kill(self) -> None:
        self.killed = True
        self.alive = False


class Rig:
    def __init__(self, alive: bool = False) -> None:
        self.clock = FakeClock()
        self.alive = alive  # how the next child is born
        self.spawned_at: list[int] = []
        self.children: list[FakeChild] = []
        self.states: list[str] = []
        self.sup = ChildSupervisor("child", self._spawn, self.clock, self.states.append)

    def _spawn(self) -> FakeChild:
        self.spawned_at.append(round(self.clock.now() * 1000))
        self.children.append(FakeChild(self.alive))
        return self.children[-1]

    def run(self, ms: int, step: int = 100) -> None:
        """Poll, then let `step` ms pass, until `ms` have gone by."""
        for _ in range(ms // step):
            self.sup.poll()
            self.clock.advance(step)
        self.sup.poll()


def test_supervisor_restarts_with_backoff():
    rig = Rig()
    rig.sup.start()
    rig.run(3700)
    assert rig.spawned_at == [0, 200, 700, 1700, 3700]


def test_supervisor_backoff_is_capped_at_2s():
    # Each child lives 20 s: never 5 deaths in 60 s, never 60 s alive, so the delay only grows.
    rig = Rig(alive=True)
    rig.sup.start()
    delays = []
    for _ in range(6):
        rig.run(20000, step=500)
        rig.children[-1].alive = False
        died = round(rig.clock.now() * 1000)
        rig.run(2500, step=100)
        delays.append(rig.spawned_at[-1] - died)
    assert delays == [200, 500, 1000, 2000, 2000, 2000]
    assert "failed" not in rig.states


def test_supervisor_reports_failed_after_5_deaths_in_60s():
    rig = Rig()
    rig.sup.start()
    rig.run(3700)  # the fifth child is born dead as well
    assert rig.states == ["up", "restarting", "up", "restarting", "up", "restarting", "up",
                          "restarting", "up", "failed"]
    # It keeps trying, every 30 s, and says nothing more while that fails too.
    rig.run(61000, step=500)
    assert rig.spawned_at[5:] == [33700, 63700]
    assert rig.states[-1] == "failed" and rig.states.count("failed") == 1


def test_supervisor_recovers_from_failed():
    rig = Rig()
    rig.sup.start()
    rig.run(3700)
    assert rig.states[-1] == "failed"
    rig.alive = True
    rig.run(30500, step=500)
    assert rig.states[-1] == "up" and len(rig.spawned_at) == 6
    # A death soon after is still the slow lane: one more death, not a fresh fast backoff.
    rig.children[-1].alive = False
    rig.alive = False
    rig.run(1000, step=500)
    assert rig.states[-1] == "failed"
    rig.run(29000, step=500)
    assert len(rig.spawned_at) == 7


def test_supervisor_backoff_resets_after_60s_alive():
    rig = Rig()
    rig.sup.start()
    rig.run(600)  # two deaths
    rig.alive = True
    rig.clock.advance(100)  # the third child is born at 700 ms and lives
    assert rig.spawned_at == [0, 200, 700]
    rig.run(60500, step=500)
    assert len(rig.spawned_at) == 3
    rig.children[-1].alive = False
    died = round(rig.clock.now() * 1000)
    rig.run(1000)
    assert rig.spawned_at[3] == died + 200  # back to the first delay, not 1 s


def test_supervisor_treats_a_spawn_error_as_a_death():
    clock, states, calls = FakeClock(), [], []

    def spawn():
        calls.append(clock.now())
        raise FileNotFoundError("no such binary")

    sup = ChildSupervisor("child", spawn, clock, states.append)
    sup.start()
    assert states == ["restarting"]
    clock.advance(200)
    assert len(calls) == 2


def test_supervisor_stop_terminates_and_never_restarts():
    rig = Rig(alive=True)
    rig.sup.start()
    rig.sup.stop()
    child = rig.children[0]
    assert child.terminated and not child.killed
    rig.run(5000, step=500)
    assert len(rig.children) == 1


def test_supervisor_stop_cancels_a_pending_restart():
    rig = Rig()
    rig.sup.start()
    rig.sup.poll()  # dead: restart due in 200 ms
    rig.sup.stop()
    rig.clock.advance(5000)
    assert len(rig.children) == 1


def test_supervisor_stop_kills_a_child_that_ignores_terminate():
    rig = Rig(alive=True)
    rig.sup.start()
    rig.children[0].stubborn = True
    rig.sup.stop()
    assert rig.children[0].killed


def test_supervisor_alive_flag():
    rig = Rig(alive=True)
    assert not rig.sup.alive
    rig.sup.start()
    assert rig.sup.alive
    rig.children[0].alive = False
    assert not rig.sup.alive
