import threading
from dataclasses import replace
from datetime import date

import pytest

from glance.backends.base import Analysis, BackendError, Ctx, Health
from glance.backends.budget import Budget
from glance.backends.supervisor import TEST_SENTENCE, AiUnavailable, BackendSupervisor, Breaker
from glance.config import BackendCfg, load_config
from glance.daemon import power_dbus
from tests.fakes import FakeClock

CTX = Ctx(profile="default", target_tone="neutral", strictness="normal", app_label="test")
OK = Analysis(tone="neutral", mismatch=False, reason=None, issues=())
SIGNED_IN = Health(True, True, "signed in")
SIGNED_OUT = Health(True, False, "signed out")


class StubBackend:
    """Backend double: `fail` is an error kind raised by every call; `calls` counts attempts."""

    def __init__(self, name: str, fail: str | None = None, health: Health = SIGNED_IN,
                 deltas: tuple[str, ...] = ("Hi ", "there")) -> None:
        self.name = name
        self.fail = fail
        self.fail_after_first_delta: str | None = None
        self.health_value: Health | Exception = health
        self.deltas = deltas
        self.calls = 0
        self.health_calls = 0
        self.cancels = 0
        self.texts: list[str] = []
        self.gate: threading.Event | None = None  # when set, calls block until released
        self.entered = threading.Semaphore(0)
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self.running = 0
        self.max_running = 0

    def _enter(self, text: str) -> None:
        with self._lock:
            self.calls += 1
            self.texts.append(text)
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        self.entered.release()
        try:
            if self.gate is not None:
                while not self.gate.wait(0.005):
                    if self._cancelled.is_set():
                        raise BackendError("cancelled")
            if self.fail:
                raise BackendError(self.fail)
        finally:
            with self._lock:
                self.running -= 1

    def analyze(self, text, ctx):
        self._enter(text)
        return replace(OK, reason=self.name)

    def rewrite(self, text, tone, ctx, custom=None):
        self._enter(text)
        for i, d in enumerate(self.deltas):
            if i == 1 and self.fail_after_first_delta:
                raise BackendError(self.fail_after_first_delta)
            yield d

    def complete(self, prefix, ctx):
        self._enter(prefix)
        yield from self.deltas

    def cancel(self):
        self.cancels += 1
        self._cancelled.set()

    def health(self):
        self.health_calls += 1
        if isinstance(self.health_value, Exception):
            raise self.health_value
        return self.health_value


class Rig:
    def __init__(self, tmp_path, cfg: BackendCfg, backends=None, speculative=None,
                 battery: bool = False) -> None:
        self.cfg = cfg
        self.clock = FakeClock()
        self.battery = battery
        self.statuses: list[str] = []
        self.config_path = tmp_path / "config.toml"
        self.backends = backends if backends is not None else {
            n: StubBackend(n) for n in ("claude", "codex", "gemini", "api")}
        if "codex" in self.backends and backends is None:
            self.backends["codex"].analyze_default = False  # as CodexBackend declares
        self.budget = Budget(self.clock, lambda: self.cfg, tmp_path / "usage.json",
                             lambda: date(2026, 11, 25))
        self.sup = BackendSupervisor(
            self.backends, lambda: self.cfg, self.budget, self.clock,
            on_battery=lambda: self.battery, on_status=self.statuses.append,
            speculative=speculative, config_path=self.config_path,
        )


def reason_of(fn) -> str:
    with pytest.raises(AiUnavailable) as e:
        fn()
    return e.value.reason


# ---------------------------------------------------------------- breaker
def test_breaker_opens_after_three_and_half_opens():
    clock = FakeClock()
    b = Breaker(clock)
    assert b.ok()
    b.failure()
    b.failure()
    assert b.ok()  # two failures: still closed
    b.failure()
    assert not b.ok()
    clock.advance(59_000)
    assert not b.ok()
    clock.advance(1_000)
    assert b.ok()  # cooldown over: exactly one trial call
    assert not b.ok()
    b.failure()  # trial failed: open for another full cooldown
    clock.advance(59_000)
    assert not b.ok()
    clock.advance(1_000)
    assert b.ok()
    b.success()  # trial succeeded: closed again
    assert b.ok() and b.ok()
    b.failure()
    b.failure()
    assert b.ok()


def test_breaker_success_resets_consecutive_count():
    b = Breaker(FakeClock())
    b.failure()
    b.failure()
    b.success()
    b.failure()
    b.failure()
    assert b.ok()


def test_supervisor_stops_calling_a_broken_backend(tmp_path):
    r = Rig(tmp_path, BackendCfg(choice="claude", per_minute=100))
    claude = r.backends["claude"]
    claude.fail = "timeout"
    for _ in range(5):
        assert reason_of(lambda: r.sup.analyze("x", CTX)) == "failed"
    assert claude.calls == 3  # breaker open: attempts 4 and 5 never reach the CLI
    assert r.budget.used_today() == 3
    claude.fail = None
    r.clock.advance(60_000)
    assert r.sup.analyze("x", CTX).reason == "claude"  # trial call closes it again
    assert r.sup.status() == "ok"


# ---------------------------------------------------------------- fallback
def test_fallback_only_to_enabled(tmp_path):
    r = Rig(tmp_path, BackendCfg(choice="claude", enabled=("api",)))
    r.backends["claude"].fail = "unavailable"
    assert r.sup.analyze("hello there", CTX).reason == "api"
    assert r.backends["claude"].calls == 1 and r.backends["api"].calls == 1
    assert r.backends["gemini"].calls == 0  # healthy, but the user did not enable it
    assert r.backends["codex"].calls == 0
    assert "".join(r.sup.rewrite("hello there", "shorter", CTX)) == "Hi there"
    assert r.backends["gemini"].calls == 0


@pytest.mark.parametrize("kind", ["timeout", "unavailable", "bad_output", "rate_limit"])
def test_each_failure_kind_falls_back_in_enabled_order(tmp_path, kind):
    r = Rig(tmp_path, BackendCfg(choice="claude", enabled=("gemini", "api")))
    r.backends["claude"].fail = kind
    r.backends["gemini"].fail = kind
    assert r.sup.analyze("x", CTX).reason == "api"
    assert [r.backends[n].calls for n in ("claude", "gemini", "api")] == [1, 1, 1]


def test_no_fallback_configured_raises_failed(tmp_path):
    r = Rig(tmp_path, BackendCfg(choice="claude"))
    r.backends["claude"].fail = "unavailable"
    assert reason_of(lambda: r.sup.analyze("x", CTX)) == "failed"
    assert reason_of(lambda: list(r.sup.rewrite("x", "shorter", CTX))) == "failed"
    assert sum(b.calls for n, b in r.backends.items() if n != "claude") == 0
    assert r.sup.status() == "ok"  # a flaky backend is not "no AI connected"


def test_codex_is_rewrite_only_by_default(tmp_path):
    r = Rig(tmp_path, BackendCfg(choice="codex", enabled=("claude",)))
    assert r.sup.analyze("x", CTX).reason == "claude"
    assert r.backends["codex"].calls == 0
    assert "".join(r.sup.rewrite("x", "shorter", CTX)) == "Hi there"
    assert r.backends["codex"].calls == 1
    # codex alone: rewrites work, analysis is simply not available (and costs nothing)
    r2 = Rig(tmp_path / "b", BackendCfg(choice="codex"))
    assert r2.sup.status() == "ok" and r2.sup.can_analyze() is False
    assert reason_of(lambda: r2.sup.analyze("x", CTX)) == "failed"
    assert r2.backends["codex"].calls == 0 and r2.budget.used_today() == 0
    # config override routes analyze to codex
    r3 = Rig(tmp_path / "c", BackendCfg(choice="codex", analyze_with=("codex",)))
    assert r3.sup.can_analyze() is True
    assert r3.sup.analyze("x", CTX).reason == "codex"


def test_rewrite_falls_back_only_before_the_first_delta(tmp_path):
    r = Rig(tmp_path, BackendCfg(choice="claude", enabled=("api",)))
    r.backends["claude"].fail_after_first_delta = "timeout"
    got: list[str] = []
    with pytest.raises(AiUnavailable) as e:
        for d in r.sup.rewrite("x", "shorter", CTX):
            got.append(d)  # noqa: PERF402 - the stream raises part-way
    assert e.value.reason == "failed"
    assert got == ["Hi "]  # never a second backend's text glued onto a partial rewrite
    assert r.backends["api"].calls == 0


def test_cancelled_is_not_a_failure_and_does_not_fall_back(tmp_path):
    r = Rig(tmp_path, BackendCfg(choice="claude", enabled=("api",), per_minute=100))
    r.backends["claude"].fail = "cancelled"
    for _ in range(4):
        with pytest.raises(BackendError) as e:
            r.sup.analyze("x", CTX)
        assert e.value.kind == "cancelled"
    assert r.backends["api"].calls == 0 and r.backends["claude"].calls == 4  # breaker closed


# ---------------------------------------------------------------- sign-out / "none"
def test_auth_failure_flips_to_none_without_retries(tmp_path):
    r = Rig(tmp_path, BackendCfg(choice="claude"))
    claude = r.backends["claude"]
    assert r.sup.status() == "ok"
    claude.fail = "auth"
    claude.health_value = SIGNED_OUT
    assert reason_of(lambda: r.sup.analyze("x", CTX)) == "none"
    assert r.sup.status() == "none"
    assert r.statuses == ["none"]
    # no spinner -> breaker -> spinner: the signed-out backend is not called again
    assert reason_of(lambda: r.sup.analyze("x", CTX)) == "none"
    assert reason_of(lambda: list(r.sup.rewrite("x", "shorter", CTX))) == "none"
    assert claude.calls == 1
    assert r.budget.used_today() == 1
    assert r.sup.auto_allowed() is False
    # still signed out on a refresh: stays "none"
    assert r.sup.healths(refresh=True)["claude"].signed_in is False
    assert reason_of(lambda: r.sup.analyze("x", CTX)) == "none"
    assert claude.calls == 1
    # the user signs in again
    claude.fail = None
    claude.health_value = SIGNED_IN
    assert r.sup.healths(refresh=True)["claude"].signed_in is True
    assert r.sup.status() == "ok"
    assert r.statuses == ["none", "ok"]
    assert r.sup.analyze("x", CTX).reason == "claude"
    assert claude.calls == 2


def test_auth_failure_tries_the_next_enabled_backend(tmp_path):
    r = Rig(tmp_path, BackendCfg(choice="claude", enabled=("api",)))
    r.backends["claude"].fail = "auth"
    r.backends["claude"].health_value = SIGNED_OUT
    assert r.sup.analyze("x", CTX).reason == "api"
    assert r.sup.status() == "ok" and r.statuses == []  # api still serves the user
    assert r.sup.active() == "api"
    r.sup.analyze("x", CTX)
    assert r.backends["claude"].calls == 1


@pytest.mark.parametrize("kind", ["bad_output", "unavailable", "rate_limit"])
def test_failure_from_signed_out_cli_is_treated_as_auth(tmp_path, kind):
    # P2-6: a signed-out CLI may fail with unrecognised text; health() decides.
    r = Rig(tmp_path, BackendCfg(choice="claude"))
    claude = r.backends["claude"]
    assert r.sup.status() == "ok"  # health cached as signed in
    claude.fail = kind
    claude.health_value = SIGNED_OUT
    assert reason_of(lambda: list(r.sup.rewrite("x", "shorter", CTX))) == "none"
    assert r.sup.status() == "none" and r.statuses == ["none"]
    assert reason_of(lambda: r.sup.analyze("x", CTX)) == "none"
    assert claude.calls == 1


def test_timeout_does_not_probe_health(tmp_path):
    r = Rig(tmp_path, BackendCfg(choice="claude"))
    claude = r.backends["claude"]
    claude.fail = "timeout"
    assert reason_of(lambda: r.sup.analyze("x", CTX)) == "failed"
    assert claude.health_calls == 0


def test_failure_health_probe_is_rate_limited(tmp_path):
    r = Rig(tmp_path, BackendCfg(choice="claude", per_minute=100))
    claude = r.backends["claude"]
    claude.fail = "bad_output"
    for _ in range(3):
        assert reason_of(lambda: r.sup.analyze("x", CTX)) == "failed"
    assert claude.health_calls == 1


def test_status_none_when_no_choice(tmp_path):
    r = Rig(tmp_path, BackendCfg())
    assert r.sup.status() == "none"
    assert r.sup.active() is None
    assert r.sup.auto_allowed() is False
    assert reason_of(lambda: r.sup.analyze("x", CTX)) == "none"
    assert reason_of(lambda: list(r.sup.rewrite("x", "shorter", CTX))) == "none"
    assert sum(b.calls for b in r.backends.values()) == 0
    # enabled alone is not a choice; an unknown choice is not a backend
    r.cfg = BackendCfg(enabled=("claude",))
    assert r.sup.status() == "none"
    r.cfg = BackendCfg(choice="ollama")
    assert r.sup.status() == "none"


def test_status_none_when_nothing_is_signed_in_or_installed(tmp_path):
    r = Rig(tmp_path, BackendCfg(choice="claude", enabled=("codex",)))
    r.backends["claude"].health_value = SIGNED_OUT
    r.backends["codex"].health_value = Health(False, None, "codex CLI not installed")
    assert r.sup.status() == "none"
    assert reason_of(lambda: r.sup.analyze("x", CTX)) == "none"
    assert reason_of(lambda: list(r.sup.rewrite("x", "shorter", CTX))) == "none"
    assert r.backends["claude"].calls == 0 and r.backends["codex"].calls == 0
    assert r.backends["gemini"].health_calls == 0  # not enabled: not even probed for status
    r.backends["codex"].health_value = SIGNED_IN
    r.clock.advance(61_000)  # health cache expires
    assert r.sup.status() == "ok" and r.sup.active() == "codex"


def test_unknown_sign_in_state_counts_as_connected(tmp_path):
    r = Rig(tmp_path, BackendCfg(choice="claude"))
    r.backends["claude"].health_value = Health(True, None, "unrecognised auth status")
    assert r.sup.status() == "ok"


def test_health_exception_means_not_connected(tmp_path):
    r = Rig(tmp_path, BackendCfg(choice="api"))
    r.backends["api"].health_value = RuntimeError("no keyring")
    assert r.sup.status() == "none"
    h = r.sup.healths()["api"]
    assert h.signed_in is False and "no keyring" not in h.detail


def test_healths_cached_60s(tmp_path):
    r = Rig(tmp_path, BackendCfg(choice="claude"))
    claude = r.backends["claude"]
    assert set(r.sup.healths()) == {"claude", "codex", "gemini", "api"}
    r.sup.healths()
    r.sup.status()
    assert claude.health_calls == 1
    r.clock.advance(59_000)
    r.sup.healths()
    assert claude.health_calls == 1
    r.clock.advance(1_000)
    r.sup.healths()
    assert claude.health_calls == 2
    r.sup.healths(refresh=True)
    assert claude.health_calls == 3


# ---------------------------------------------------------------- budget
def test_limit_status_and_auto_allowed(tmp_path):
    r = Rig(tmp_path, BackendCfg(choice="claude", per_day=2))
    assert r.sup.auto_allowed() is True
    r.sup.analyze("x", CTX)
    assert r.sup.status() == "ok" and r.statuses == []
    r.sup.analyze("x", CTX)
    assert r.sup.status() == "limit"
    assert r.statuses == ["limit"]  # pushed as soon as the last call is spent
    assert r.sup.auto_allowed() is False
    assert reason_of(lambda: r.sup.analyze("x", CTX)) == "limit"
    # explicit clicks count too
    assert reason_of(lambda: list(r.sup.rewrite("x", "shorter", CTX))) == "limit"
    assert r.backends["claude"].calls == 2


def test_minute_limit_recovers(tmp_path):
    r = Rig(tmp_path, BackendCfg(choice="claude", per_minute=1))
    r.sup.analyze("x", CTX)
    assert r.sup.status() == "limit"
    r.clock.advance(60_000)
    assert r.sup.status() == "ok" and r.statuses == ["limit", "ok"]
    r.sup.analyze("x", CTX)


def test_none_wins_over_limit(tmp_path):
    r = Rig(tmp_path, BackendCfg(per_day=0))
    assert r.sup.status() == "none"


def test_battery_blocks_auto_but_not_explicit(tmp_path):
    r = Rig(tmp_path, BackendCfg(choice="claude"), battery=True)
    assert r.sup.status() == "ok"
    assert r.sup.auto_allowed() is False
    assert "".join(r.sup.rewrite("x", "shorter", CTX)) == "Hi there"
    assert r.sup.analyze("x", CTX).reason == "claude"
    r.battery = False
    assert r.sup.auto_allowed() is True


def test_battery_probe_error_does_not_break_auto(tmp_path):
    r = Rig(tmp_path, BackendCfg(choice="claude"))

    def boom() -> bool:
        raise OSError("no bus")

    sup = BackendSupervisor(r.backends, lambda: r.cfg, r.budget, r.clock, on_battery=boom,
                            on_status=r.statuses.append)
    assert sup.auto_allowed() is True


def test_speculate_default_only_for_api(tmp_path):
    r = Rig(tmp_path, BackendCfg(choice="claude"))
    assert r.sup.speculate() is False
    r.cfg = BackendCfg(choice="api")
    assert r.sup.speculate() is True
    r.cfg = BackendCfg(choice="claude", speculate=True)
    assert r.sup.speculate() is True
    r.cfg = BackendCfg(choice="api", speculate=False)
    assert r.sup.speculate() is False
    r.cfg = BackendCfg()
    assert r.sup.speculate() is False


def test_every_attempt_spends_budget(tmp_path):
    r = Rig(tmp_path, BackendCfg(choice="claude", enabled=("gemini", "api")))
    r.backends["claude"].fail = "timeout"
    r.backends["gemini"].fail = "unavailable"
    r.sup.analyze("x", CTX)
    assert r.budget.used_today() == 3  # two failed attempts + the one that answered
    list(r.sup.rewrite("x", "shorter", CTX))
    assert r.budget.used_today() == 6
    r.sup.test("api")
    assert r.budget.used_today() == 7


def test_fallback_stops_when_budget_runs_out_mid_chain(tmp_path):
    r = Rig(tmp_path, BackendCfg(choice="claude", enabled=("api",), per_day=1))
    r.backends["claude"].fail = "timeout"
    assert reason_of(lambda: r.sup.analyze("x", CTX)) == "limit"
    assert r.backends["api"].calls == 0


# ---------------------------------------------------------------- use / test / cancel
def test_use_writes_config_preserving_comments(tmp_path):
    r = Rig(tmp_path, BackendCfg(choice="claude"))
    r.config_path.write_text("# mine\n[general]\nchip_idle_ms = 500\n")
    r.sup.use("codex")
    assert r.config_path.read_text().startswith("# mine\n[general]\nchip_idle_ms = 500\n")
    c = load_config(r.config_path)
    assert c.backend.choice == "codex" and c.general.chip_idle_ms == 500
    with pytest.raises(ValueError):
        r.sup.use("ollama")
    assert load_config(r.config_path).backend.choice == "codex"


def test_test_reports_ok_and_detail(tmp_path):
    r = Rig(tmp_path, BackendCfg(choice="claude"))
    assert r.sup.test("gemini") == (True, "ok")  # any backend can be tested, chosen or not
    assert r.backends["gemini"].texts == [TEST_SENTENCE]
    assert r.backends["claude"].calls == 0
    r.backends["api"].fail = "auth"
    assert r.sup.test("api") == (False, "auth")
    r.backends["claude"].fail = "timeout"
    assert r.sup.test("claude") == (False, "timeout")
    assert r.sup.test("ollama") == (False, "unknown backend")
    # rewrite-only backends are tested with a rewrite
    assert r.sup.test("codex") == (True, "ok")
    assert r.backends["codex"].texts == [TEST_SENTENCE]
    r.backends["codex"].deltas = ()
    assert r.sup.test("codex") == (False, "bad_output")


def test_test_respects_the_limit(tmp_path):
    r = Rig(tmp_path, BackendCfg(choice="claude", per_day=0))
    assert r.sup.test("claude") == (False, "limit")
    assert r.backends["claude"].calls == 0


def test_successful_test_reconnects_a_signed_out_backend(tmp_path):
    r = Rig(tmp_path, BackendCfg(choice="claude"))
    claude = r.backends["claude"]
    claude.fail = "auth"
    assert reason_of(lambda: r.sup.analyze("x", CTX)) == "none"
    claude.fail = None
    assert r.sup.test("claude") == (True, "ok")
    assert r.sup.status() == "ok" and r.statuses == ["none", "ok"]


def test_cancel_all_cancels_every_backend_in_both_lanes(tmp_path):
    spec = {"api": StubBackend("api")}
    r = Rig(tmp_path, BackendCfg(choice="api"), speculative=spec)
    r.sup.cancel_all()
    assert all(b.cancels == 1 for b in r.backends.values())
    assert spec["api"].cancels == 1


# ---------------------------------------------------------------- lanes (P2-7)
def _spawn(fn):
    box: dict[str, object] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except Exception as e:  # noqa: BLE001 - handed back to the test thread
            box["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, box


def test_one_explicit_and_one_speculative_lane(tmp_path):
    main, spec = StubBackend("api"), StubBackend("api")
    main.gate, spec.gate = threading.Event(), threading.Event()
    r = Rig(tmp_path, BackendCfg(choice="api"), backends={"api": main}, speculative={"api": spec})

    t1, b1 = _spawn(lambda: r.sup.analyze("one", CTX))
    assert main.entered.acquire(timeout=2)
    t2, b2 = _spawn(lambda: "".join(r.sup.rewrite("two", "shorter", CTX)))
    # the speculative lane runs alongside the explicit one, on its own backend instance
    t3, b3 = _spawn(lambda: "".join(r.sup.rewrite("three", "shorter", CTX, speculative=True)))
    assert spec.entered.acquire(timeout=2)
    # a second speculative call waits for the speculative lane
    t4, b4 = _spawn(lambda: r.sup.analyze("four", CTX, speculative=True))
    t4.join(0.1)
    assert main.calls == 1 and spec.calls == 1  # "two" and "four" are queued, not running
    assert main.cancels == 0 and spec.cancels == 0

    spec.gate.set()
    t3.join(2)
    t4.join(2)
    assert b3["value"] == "Hi there" and b4["value"].reason == "api"

    # an explicit call preempts in-flight speculation, and only speculation
    spec.gate.clear()
    t5, b5 = _spawn(lambda: r.sup.analyze("five", CTX, speculative=True))
    assert spec.entered.acquire(timeout=2)  # "four"
    assert spec.entered.acquire(timeout=2)  # "five" is now in flight
    main.gate.set()  # "one" finishes; "two" takes the explicit lane and preempts "five"
    for t in (t1, t2, t5):
        t.join(2)
    assert b1["value"].reason == "api" and b2["value"] == "Hi there"
    assert isinstance(b5["error"], BackendError) and b5["error"].kind == "cancelled"
    assert main.cancels == 0 and spec.cancels == 1
    assert main.max_running == 1 and spec.max_running == 1
    assert main.texts == ["one", "two"] and spec.texts == ["three", "four", "five"]


def test_speculative_needs_its_own_instance_and_auto_allowed(tmp_path):
    r = Rig(tmp_path, BackendCfg(choice="api"))  # no speculative instances wired
    assert reason_of(lambda: r.sup.analyze("x", CTX, speculative=True)) == "failed"
    assert r.backends["api"].calls == 0 and r.budget.used_today() == 0

    spec = {"api": StubBackend("api")}
    r2 = Rig(tmp_path / "b", BackendCfg(choice="api", per_day=1), speculative=spec, battery=True)
    assert reason_of(lambda: r2.sup.analyze("x", CTX, speculative=True)) == "battery"
    r2.battery = False
    assert r2.sup.analyze("x", CTX, speculative=True).reason == "api"
    assert reason_of(lambda: r2.sup.analyze("x", CTX, speculative=True)) == "limit"
    assert spec["api"].calls == 1 and r2.backends["api"].calls == 0
    r3 = Rig(tmp_path / "c", BackendCfg(), speculative=spec)
    assert reason_of(lambda: list(r3.sup.rewrite("x", "shorter", CTX, speculative=True))) == "none"


def test_automatic_analyze_does_not_preempt_a_live_completion(tmp_path):
    main, spec = StubBackend("api"), StubBackend("api", deltas=("to", "morrow."))
    spec.gate = threading.Event()
    r = Rig(tmp_path, BackendCfg(choice="api"), backends={"api": main}, speculative={"api": spec})

    live, box = _spawn(lambda: "".join(r.sup.complete("see you", CTX, speculative=True)))
    assert spec.entered.acquire(timeout=2)  # the live completion is in flight
    assert r.sup.analyze("see you", CTX).reason == "api"  # the 1.2 s analysis, main lane
    assert spec.cancels == 0 and live.is_alive()
    spec.gate.set()
    live.join(2)
    assert box["value"] == "tomorrow."  # it survived and is delivered


@pytest.mark.parametrize("call", [
    lambda sup: "".join(sup.rewrite("x", "shorter", CTX)),
    lambda sup: "".join(sup.complete("see you", CTX)),  # on demand
    lambda sup: sup.analyze("x", CTX, explicit=True),
    lambda sup: sup.test("api"),
])
def test_explicit_calls_still_preempt_a_live_completion(tmp_path, call):
    main, spec = StubBackend("api"), StubBackend("api")
    spec.gate = threading.Event()
    r = Rig(tmp_path, BackendCfg(choice="api"), backends={"api": main}, speculative={"api": spec})

    live, box = _spawn(lambda: "".join(r.sup.complete("see you", CTX, speculative=True)))
    assert spec.entered.acquire(timeout=2)
    call(r.sup)
    live.join(2)
    assert spec.cancels == 1 and main.cancels == 0
    assert isinstance(box["error"], BackendError) and box["error"].kind == "cancelled"


def test_abandoned_rewrite_releases_the_lane(tmp_path):
    r = Rig(tmp_path, BackendCfg(choice="claude"))
    it = r.sup.rewrite("x", "shorter", CTX)
    assert next(it) == "Hi "
    it.close()  # the preview was discarded mid-stream
    assert r.sup.analyze("x", CTX).reason == "claude"


def test_busy_lane_times_out_instead_of_deadlocking(tmp_path):
    r = Rig(tmp_path, BackendCfg(choice="claude"))
    r.sup.lane_wait_s = 0.05
    it = r.sup.rewrite("x", "shorter", CTX)
    next(it)  # this thread holds the explicit lane
    assert reason_of(lambda: r.sup.analyze("x", CTX)) == "failed"
    assert r.backends["claude"].calls == 1


# ---------------------------------------------------------------- power
def test_on_battery_reads_upower_and_is_false_on_any_error():
    assert power_dbus.on_battery(read=lambda: True) is True
    assert power_dbus.on_battery(read=lambda: False) is False

    def boom() -> bool:
        raise RuntimeError("no system bus")

    assert power_dbus.on_battery(read=boom) is False
    assert power_dbus.on_battery(read=lambda: "yes") is False
