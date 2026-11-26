"""Backend supervisor: selection, health, budget, circuit breaking, fallback and lanes."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from glance.backends import auto_choice, cli_path
from glance.backends.base import (
    ANALYZE_TIMEOUT_S,
    CHECK_TIMEOUT_S,
    Analysis,
    Backend,
    BackendError,
    Ctx,
    Health,
    analyze_budget,
)
from glance.backends.budget import Budget
from glance.backends.partial import analyze_progress
from glance.backends.usage import Usage, UsageLedger
from glance.clock import Clock
from glance.config import CLI_MODEL_BACKENDS, BackendCfg, update_config
from glance.log import log_event
from glance.paths import config_path as default_config_path

TEST_SENTENCE = "Teh quick brown fox jump over the lazy dog, i think its fine."
TEST_TONE = "professional"
TEST_CTX = Ctx(profile="default", target_tone="neutral", strictness="normal", app_label="Glance")
HEALTH_TTL_S = 60.0
HEALTH_WAIT_S = 5.0  # all backends are asked at once; one that is slower is "timed out"
LANE_WAIT_S = 30.0
REASONS = ("none", "limit", "battery", "failed")


class AiUnavailable(Exception):
    """No backend could serve the call. `reason` is one of none | limit | battery | failed."""

    def __init__(self, reason: str) -> None:
        if reason not in REASONS:
            raise ValueError(f"unknown reason: {reason}")
        super().__init__(reason)
        self.reason = reason


class Breaker:
    """Opens after `threshold` consecutive failures; one trial call per cooldown after that."""

    def __init__(self, clock: Clock, threshold: int = 3, cooldown_s: int = 60) -> None:
        self._clock, self._threshold, self._cooldown_s = clock, threshold, cooldown_s
        self._fails = 0
        self._opened_at = 0.0

    def ok(self) -> bool:
        """May a call go out now? A True answer while open hands out the single trial."""
        if self._fails < self._threshold:
            return True
        now = self._clock.now()
        if now - self._opened_at >= self._cooldown_s:
            self._opened_at = now  # an unanswered trial is offered again after a cooldown
            return True
        return False

    def success(self) -> None:
        self._fails = 0

    def failure(self) -> None:
        self._fails += 1
        if self._fails >= self._threshold:
            self._opened_at = self._clock.now()


class BackendSupervisor:
    """The one door to AI backends.

    Order is `cfg.choice`, then `cfg.enabled` in order: nothing the user did not enable is
    ever called. Every attempt spends budget. Calls run one at a time in the main lane;
    `speculative=True` calls run one at a time in a second lane, on the separate backend
    instances given as `speculative` (a backend's `cancel()` stops everything running on that
    instance, so the lanes must not share one), and are cancelled when a main-lane call the
    user asked for starts: a rewrite, an on-demand completion, `test()`. The automatic
    `analyze` does not preempt (a live completion started in the same pause must survive it)
    unless it is called with `explicit=True`.

    A backend counts as not connected ("down") after an `auth` error or a health check that
    says not installed / signed out, until a health check says signed in or `test()` succeeds.

    Methods block (subprocess / HTTPS, health checks included): call them off the UI thread.
    `on_status` fires on the calling thread whenever the value of `status()` is seen to
    change (the starting value is taken to be "ok"). A `cancelled` BackendError passes
    through unchanged.
    """

    def __init__(
        self,
        backends: dict[str, Backend],
        cfg: Callable[[], BackendCfg],
        budget: Budget,
        clock: Clock,
        on_battery: Callable[[], bool],
        on_status: Callable[[str], None],
        speculative: dict[str, Backend] | None = None,
        config_path: Path | None = None,
        ledger: UsageLedger | None = None,
        cost_limit: Callable[[], float | None] = lambda: None,
    ) -> None:
        self._backends, self._cfg, self._budget, self._clock = backends, cfg, budget, clock
        self._on_battery, self._on_status = on_battery, on_status
        self._spec = dict(speculative or {})
        self._config_path = config_path
        self.ledger, self._cost_limit = ledger, cost_limit
        self.lane_wait_s = LANE_WAIT_S
        self._lock = threading.RLock()  # guards the state below, never held during a call
        self._breakers = {name: Breaker(clock) for name in backends}
        self._down: set[str] = set()
        self._health: dict[str, tuple[float, Health]] = {}
        self._probed_at: dict[str, float] = {}
        self._last_status = "ok"
        self._sync_lock = threading.Lock()
        self._main_lane = threading.Lock()
        self._spec_lane = threading.Lock()
        self._spec_running: Backend | None = None
        self._fallback: str | None = None  # auto-chosen stand-in for a missing/broken choice
        self._calls = threading.local()  # per thread: the last failure's kind

    def last_error(self) -> str | None:
        """The kind of the last backend failure (BackendError.kind) seen by a call on this
        thread since its `analyze` began; None when none failed."""
        return getattr(self._calls, "error", None)

    # -- per-call settings ---------------------------------------------------------
    def sync(self, choice: str | None = None) -> None:
        """Apply the configured models, and keep the claude warm process on only while claude
        is the chosen backend (`choice` overrides the config's, for a switch not yet reloaded).
        Switching away, `warm = false` and a model change all stop the idle processes."""
        with self._sync_lock:
            cfg = self._cfg()
            chosen = choice or cfg.choice
            for pool, main in ((self._backends, True), (self._spec, False)):
                for name, backend in pool.items():
                    if name in CLI_MODEL_BACKENDS and hasattr(backend, "set_model"):
                        backend.set_model(cfg.models.get(name, ""))
                    if hasattr(backend, "set_warm"):
                        # the speculative twin never warms: that would double the memory
                        backend.set_warm(main and name == "claude" and chosen == "claude"
                                         and cfg.warm is not False)

    # -- selection -------------------------------------------------------------
    def _order(self) -> list[str]:
        cfg = self._cfg()
        names: list[str] = []
        if cfg.choice is not None and cfg.choice in self._backends:
            names = [cfg.choice]
            names += [n for n in cfg.enabled if n in self._backends and n not in names]
        fallback = self._fallback
        if fallback is not None and fallback in self._backends and fallback not in names:
            names.append(fallback)
        return names

    def _connected(self) -> list[str]:
        with self._lock:
            return [n for n in self._order() if n not in self._down]

    def _analyzes(self, name: str) -> bool:
        return (getattr(self._backends[name], "analyze_default", True)
                or name in self._cfg().analyze_with)

    def active(self) -> str | None:
        """The backend the next call goes to first, or None when nothing is connected."""
        return next(iter(self._connected()), None)

    def can_analyze(self) -> bool:
        """False when every connected backend is rewrite-only (e.g. codex alone)."""
        return any(self._analyzes(n) for n in self._connected())

    def speculate(self) -> bool:
        choice = self._cfg().speculate
        return choice if choice is not None else self.active() == "api"

    def live_autocomplete(self) -> bool:
        """Complete while the user pauses? The setting, else only on the api backend."""
        choice = self._cfg().live_autocomplete
        return choice if choice is not None else self.active() == "api"

    # -- health ----------------------------------------------------------------
    def _check_health(self, name: str, refresh: bool = False) -> Health:
        with self._lock:
            hit = self._health.get(name)
            if hit and not refresh and self._clock.now() - hit[0] < HEALTH_TTL_S:
                return hit[1]
        try:
            h = self._backends[name].health()
        except Exception as e:  # noqa: BLE001 - e.g. no keyring: not connected, never a crash
            log_event("backend_health_error", backend=name, error=e.__class__.__name__)
            h = Health(True, False, "health check failed")
        with self._lock:
            self._health[name] = (self._clock.now(), h)
            if not h.installed or h.signed_in is False:
                self._down.add(name)
            elif h.signed_in is True:
                self._down.discard(name)
            # signed_in None (unknown) neither condemns nor clears a backend
        return h

    def healths(self, refresh: bool = False) -> dict[str, Health]:
        if refresh:  # a CLI installed since start-up: ask the login shell for PATH again
            cli_path.refresh()
        names = list(self._backends)
        got: dict[str, Health] = {}

        def ask(name: str) -> None:
            got[name] = self._check_health(name, refresh)

        # at the same time: `glance backend list` waits for the slowest, not for the sum
        threads = [threading.Thread(target=ask, args=(n,), name=f"health-{n}", daemon=True)
                   for n in names]
        for t in threads:
            t.start()
        deadline = time.monotonic() + HEALTH_WAIT_S
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))
        out = {n: got.get(n) or Health(True, None, "timed out") for n in names}
        self._notify()
        return out

    # -- usage -------------------------------------------------------------------
    def cost_limited(self) -> bool:
        """Has the cost the backends reported this month reached `[ai] monthly_cost_limit_usd`?"""
        limit = self._cost_limit()
        return limit is not None and self.ledger is not None and self.ledger.month_cost() >= limit

    def limit_reason(self) -> str | None:
        """Why calls are refused: "month" (cost limit), "day", "minute" - or None."""
        if self.cost_limited():
            return "month"
        return self._budget.exhausted()

    def usage_report(self, days: int | None = None) -> dict[str, Any]:
        """The usage tables plus the limits they are measured against (never any text)."""
        cfg = self._cfg()
        report: dict[str, Any] = {
            "periods": self.ledger.periods(days) if self.ledger else {},
            "budget": {"used_today": self._budget.used_today(), "per_day": cfg.per_day,
                       "per_minute": cfg.per_minute},
            "monthly_cost_limit_usd": self._cost_limit(),
            "month_cost_usd": round(self.ledger.month_cost(), 6) if self.ledger else 0.0,
            "limit_reason": self.limit_reason()}
        return report

    def usage_today(self) -> dict[str, Any]:
        """A compact summary for `glance status`."""
        total = self.ledger.periods()["today"]["total"] if self.ledger else {
            "calls": 0, "input_tokens": 0, "output_tokens": 0, "tokens_known": False,
            "cost_usd": None}
        per_day = self._cfg().per_day
        return {"calls": total["calls"],
                "tokens": (total["input_tokens"] + total["output_tokens"]
                           if total["tokens_known"] else None),
                "cost_usd": total["cost_usd"],
                "budget_remaining": max(per_day - self._budget.used_today(), 0),
                "limit": self.limit_reason()}

    def _model_of(self, backend: Backend) -> str:
        return getattr(backend, "model", "") or getattr(backend, "_model", "") or "default"

    def _before(self, backend: Backend) -> None:
        take = getattr(backend, "take_usage", None)
        if take is not None:
            take()  # drop anything left over from a call nobody recorded

    def _record(self, name: str, backend: Backend, purpose: str, speculative: bool,
                outcome: str) -> None:
        """Count one attempt in the ledger. Must never break the AI call itself."""
        if self.ledger is None:
            return
        try:
            take = getattr(backend, "take_usage", None)
            usage: Usage | None = take() if take is not None else None
            self.ledger.record(name, self._model_of(backend), purpose, speculative,
                               outcome, usage)
        except Exception as e:  # noqa: BLE001 - bookkeeping failure must not fail a rewrite
            log_event("usage_record_error", kind=e.__class__.__name__)

    # -- status ----------------------------------------------------------------
    def _status(self) -> str:
        if not self._connected():
            return "none"
        if self._budget.exhausted() is not None or self.cost_limited():
            return "limit"
        return "ok"

    def _notify(self) -> str:
        status = self._status()
        with self._lock:
            changed, self._last_status = status != self._last_status, status
        if changed:
            log_event("ai_status", status=status)
            self._on_status(status)
        return status

    def status(self) -> str:
        for name in self._order():
            self._check_health(name)
        return self._notify()

    def auto_allowed(self) -> bool:
        return self.status() == "ok" and not self._battery()

    def _battery(self) -> bool:
        try:
            return self._on_battery() is True
        except Exception:  # noqa: BLE001 - an unknown power state must not stop AI
            return False

    # -- lanes -----------------------------------------------------------------
    @contextmanager
    def _lane(self, speculative: bool, explicit: bool = True) -> Iterator[None]:
        lane = self._spec_lane if speculative else self._main_lane
        if not lane.acquire(timeout=self.lane_wait_s):
            raise AiUnavailable("failed")
        try:
            if not speculative and explicit:
                with self._lock:
                    running = self._spec_running
                if running is not None:
                    running.cancel()  # an explicit call preempts speculation
            yield
        finally:
            lane.release()

    @contextmanager
    def _running(self, backend: Backend, speculative: bool) -> Iterator[None]:
        if speculative:
            with self._lock:
                self._spec_running = backend
        try:
            yield
        finally:
            if speculative:
                with self._lock:
                    self._spec_running = None

    # -- one call ----------------------------------------------------------------
    def _plan(self, kind: str, speculative: bool) -> list[tuple[str, Backend]]:
        self.sync()
        names = self._connected()
        if not names:
            self._notify()
            raise AiUnavailable("none")
        if self.cost_limited():
            raise AiUnavailable("limit")
        if speculative:
            if self._budget.exhausted() is not None:
                raise AiUnavailable("limit")
            if self._battery():
                raise AiUnavailable("battery")
        pool = self._spec if speculative else self._backends
        return [(n, pool[n]) for n in names
                if n in pool and (kind != "analyze" or self._analyzes(n))]

    def _begin(self, name: str) -> bool:
        """Clear one attempt on `name`: breaker first, then budget (raises when exhausted)."""
        with self._lock:
            if name in self._down or not self._breakers[name].ok():
                return False
        spent = self._budget.try_spend()
        self._notify()
        if not spent:
            raise AiUnavailable("limit")
        return True

    def _succeeded(self, name: str) -> None:
        with self._lock:
            self._breakers[name].success()
            self._down.discard(name)
        self._notify()

    def _failed(self, name: str, e: BackendError) -> None:
        """Record a failed attempt. Sign-outs take the backend down; the rest trip the breaker."""
        log_event("backend_fail", backend=name, kind=e.kind)
        self._calls.error = e.kind
        signed_out = e.kind == "auth"
        if not signed_out and e.kind != "timeout":
            # A signed-out CLI can fail with text nobody recognises (often `bad_output` on the
            # stream path), so ask health() - a fresh answer at most once per TTL per backend.
            with self._lock:
                last = self._probed_at.get(name)
                fresh = last is None or self._clock.now() - last >= HEALTH_TTL_S
                if fresh:
                    self._probed_at[name] = self._clock.now()
            signed_out = self._check_health(name, refresh=fresh).signed_in is False
        with self._lock:
            if signed_out:
                self._down.add(name)
            else:
                self._breakers[name].failure()
        self._notify()

    def _gave_up(self) -> AiUnavailable:
        return AiUnavailable("none" if self._notify() == "none" else "failed")

    def prepare(self, ctx: Ctx) -> None:
        """A field got the focus: get the next call's start-up out of the way. Health is
        refreshed (when stale) so the check does not wait for it, and the backend the next
        call goes to may start a process or open a connection (`prepare(ctx)`). Nothing of
        the field's text goes anywhere. Blocks: call it off the UI thread."""
        try:
            self.status()
            if self.cost_limited():
                return
            name = self.active()
            backend = self._backends.get(name) if name is not None else None
            prepare = getattr(backend, "prepare", None)
            if prepare is not None:
                self.sync()  # model and warm switch first: a spare is started for them
                prepare(ctx)
        except Exception as e:  # noqa: BLE001 - only a head start: never an error
            log_event("prepare_error", kind=type(e).__name__)

    def analyze(self, text: str, ctx: Ctx, speculative: bool = False,
                explicit: bool = False,
                on_partial: Callable[[str], None] | None = None) -> Analysis:
        """`explicit=True` when the user asked for this analysis: only then does it preempt,
        and each backend gets the longer CHECK_TIMEOUT_S (else ANALYZE_TIMEOUT_S).
        `on_partial` gets the corrected text as far as it has streamed in (on this thread),
        from a backend that streams it."""
        budget = CHECK_TIMEOUT_S if explicit else ANALYZE_TIMEOUT_S
        self._calls.error = None
        with self._lane(speculative, explicit), analyze_budget(budget), \
                analyze_progress(on_partial):
            for name, backend in self._plan("analyze", speculative):
                if not self._begin(name):
                    continue
                self._before(backend)
                try:
                    with self._running(backend, speculative):
                        result = backend.analyze(text, ctx)
                except BackendError as e:
                    cancelled = e.kind == "cancelled"
                    self._record(name, backend, "analyze", speculative,
                                 "cancelled" if cancelled else "failed")
                    if cancelled:
                        raise
                    self._failed(name, e)
                    continue
                self._record(name, backend, "analyze", speculative, "ok")
                self._succeeded(name)
                return result
            raise self._gave_up()

    def rewrite(self, text: str, tone: str, ctx: Ctx, custom: str | None = None,
                speculative: bool = False) -> Iterator[str]:
        """Text deltas. Falls back only until the first delta has been handed out."""
        from glance.backends.prompts import SELECTION_ACTIONS

        purpose = "sharpen" if tone == "sharpen_prompt" else \
            "select" if tone in SELECTION_ACTIONS else "rewrite"
        return self._stream(speculative, lambda b: b.rewrite(text, tone, ctx, custom), purpose)

    def complete(self, prefix: str, ctx: Ctx, speculative: bool = False) -> Iterator[str]:
        """Deltas of a continuation of `prefix`: budget, breaker and fallback as `rewrite`."""
        return self._stream(speculative, lambda b: b.complete(prefix, ctx), "complete")

    def answer_text(self, system: str, user: str, purpose: str = "shot") -> Iterator[str]:
        """A free-form answer (Glance Shot's Translate / Explain on text): budget, breaker,
        cost limit and fallback as `rewrite`. The caller shields the text."""
        return self._stream(False, lambda b: b.answer(system, user), purpose)

    def vision_backend(self) -> tuple[str, Backend] | None:
        """The connected backend an image would go to (the first in order that takes
        images), or None."""
        for name in self._connected():
            backend = self._backends.get(name)
            if backend is not None and getattr(backend, "vision", False):
                return name, backend
        return None

    def see(self, name: str, png: bytes, system: str, user: str) -> Iterator[str]:
        """An answer about an image from `name` only: an image never falls back to a backend
        the user was not told about. Budget, breaker, ledger and cost limit apply."""
        return self._stream(False, lambda b: b.see(png, system, user), "shot", only=name)

    def _stream(self, speculative: bool, call: Callable[[Backend], Iterator[str]],
                purpose: str = "rewrite", only: str | None = None) -> Iterator[str]:
        with self._lane(speculative):
            plan = self._plan("rewrite", speculative)
            if only is not None:
                plan = [(n, b) for n, b in plan if n == only]
                if not plan:
                    raise AiUnavailable("none")
            for name, backend in plan:
                if not self._begin(name):
                    continue
                started = False
                outcome = "cancelled"  # what a consumer that stops reading leaves behind
                self._before(backend)
                try:
                    try:
                        with self._running(backend, speculative):
                            for delta in call(backend):
                                started = True
                                yield delta
                        outcome = "ok"
                    except BackendError as e:
                        if e.kind == "cancelled":
                            raise
                        outcome = "failed"
                        self._record(name, backend, purpose, speculative, outcome)
                        outcome = ""  # recorded
                        self._failed(name, e)
                        if started:
                            raise self._gave_up() from e
                        continue
                finally:
                    if outcome:
                        self._record(name, backend, purpose, speculative, outcome)
                self._succeeded(name)
                return
            raise self._gave_up()

    # -- management --------------------------------------------------------------
    def use(self, name: str, auto: bool = False) -> None:
        """Write `backend.choice`. `auto`: glanced chose it, not the user (doctor says so)."""
        if name not in self._backends:
            raise ValueError(f"unknown backend: {name}")
        path = self._config_path or default_config_path()
        update_config(path, "backend.choice", name)
        auto_choice.write_marker(path, name if auto else None)
        if not auto:
            with self._lock:
                self._fallback = None
        self.sync(name)  # the config reloads later; the warm process must not wait for it

    @property
    def auto_chosen(self) -> str | None:
        """The backend standing in for a missing or broken choice, if any."""
        return self._fallback

    def auto_choose(self) -> str | None:
        """Pick an AI when `[backend] choice` is unset or the chosen one is not usable
        (installed, not signed out). Unset: the pick is written to the config, once. Broken:
        the config is left alone and the pick only stands in at run time. Returns the name
        when this call started using a new one (the caller tells the user), else None.
        Blocks (health checks): call it off the UI thread."""
        cfg = self._cfg()
        if cfg.choice in self._backends and auto_choice.usable(self._check_health(cfg.choice)):
            with self._lock:
                self._fallback = None
            return None
        name = auto_choice.pick_from(self.healths())
        with self._lock:
            if name == self._fallback:
                return None
            self._fallback = name
        if name is None:
            self._notify()
            return None
        log_event("backend_auto_chosen", backend=name, wrote=cfg.choice is None)
        if cfg.choice is None:
            update_config(self._config_path or default_config_path(), "backend.choice", name)
            auto_choice.write_marker(self._config_path or default_config_path(), name)
        self.sync(name)
        self._notify()
        return name

    def cancel_all(self) -> None:
        for backend in (*self._backends.values(), *self._spec.values()):
            backend.cancel()

    def cancel_speculative(self) -> None:
        """Stop what runs in the speculative lane; explicit calls are left alone."""
        for backend in self._spec.values():
            backend.cancel()

    def test(self, name: str) -> tuple[bool, str]:
        """One real call with the synthetic sentence, on `name` whether chosen or not."""
        backend = self._backends.get(name)
        if backend is None:
            return False, "unknown backend"
        self.sync()
        try:
            with self._lane(False):
                if self.cost_limited() or not self._budget.try_spend():
                    self._notify()
                    return False, "limit"
                self._before(backend)
                try:
                    if self._analyzes(name):
                        backend.analyze(TEST_SENTENCE, TEST_CTX)
                    elif not "".join(backend.rewrite(TEST_SENTENCE, TEST_TONE, TEST_CTX)).strip():
                        raise BackendError("bad_output", "empty rewrite")
                except BackendError as e:
                    cancelled = e.kind == "cancelled"
                    self._record(name, backend, "other", False,
                                 "cancelled" if cancelled else "failed")
                    if cancelled:
                        return False, "cancelled"
                    self._failed(name, e)
                    with self._lock:
                        return False, "auth" if name in self._down else e.kind
                self._record(name, backend, "other", False, "ok")
                self._succeeded(name)
                return True, "ok"
        except AiUnavailable:
            return False, "busy"
