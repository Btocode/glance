"""Debounced check scheduling: typing -> local check -> settled -> analyze."""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable

from glance.clock import Clock, Handle
from glance.config import GeneralCfg
from glance.protocol import FieldSnapshot

LOCAL_QUIET_MS = 80
LOCAL_BURST_QUIET_MS = 250  # local-check quiet while keys keep arriving (chip is hidden anyway)
CARET_SETTLE_MS = 30
MEANINGFUL_TOKENS = 2


def _tokens(text: str) -> Counter[str]:
    return Counter(text.casefold().split())


def _token_distance(a: Counter[str], b: Counter[str]) -> int:
    """Size of the multiset symmetric difference."""
    return sum(((a - b) + (b - a)).values())


class CheckScheduler:
    def __init__(
        self,
        clock: Clock,
        cfg: Callable[[], GeneralCfg],
        on_local: Callable[[FieldSnapshot, int], None],
        on_settled: Callable[[FieldSnapshot, int], None],
        on_analyze: Callable[[FieldSnapshot, int], None],
        on_typing: Callable[[], None],
    ) -> None:
        self._clock = clock
        self._cfg = cfg
        self._on_local = on_local
        self._on_settled = on_settled
        self._on_analyze = on_analyze
        self._on_typing = on_typing
        self.generation = 0
        self._local: Handle | None = None
        self._settled: Handle | None = None
        self._analyze: Handle | None = None
        self._last: tuple[str, str] | None = None  # (field_id, text) of the previous snapshot
        self._settled_pending = False
        self._analyzed: dict[str, Counter[str]] = {}
        self._last_change = -1e9

    # -- public -----------------------------------------------------------
    def on_snapshot(self, s: FieldSnapshot) -> None:
        if s.gated:
            self._reset()
            return
        if self._last == (s.field_id, s.text):
            if self._settled_pending:
                return  # still inside the idle wait after typing; the long timer will settle
            self._cancel_settled()
            gen = self.generation
            self._settled = self._clock.call_later(CARET_SETTLE_MS, lambda: self._fire_settled(s, gen))
            return
        self._last = (s.field_id, s.text)
        now = self._clock.now()
        bursting = (now - self._last_change) * 1000 < self._cfg().chip_idle_ms
        self._last_change = now
        self._cancel_all()
        self.generation += 1
        gen = self.generation
        cfg = self._cfg()
        self._on_typing()
        self._local = self._clock.call_later(
            LOCAL_BURST_QUIET_MS if bursting else LOCAL_QUIET_MS, lambda: self._on_local(s, gen))
        self._settled = self._clock.call_later(cfg.chip_idle_ms, lambda: self._fire_settled(s, gen))
        self._settled_pending = True
        if len(s.text) >= cfg.min_chars_analyze and self._meaningful(s):
            self._analyze = self._clock.call_later(cfg.analyze_idle_ms, lambda: self._fire_analyze(s, gen))

    def on_focus_lost(self) -> None:
        self._reset()

    # -- internals --------------------------------------------------------
    def _reset(self) -> None:
        self._cancel_all()
        self._last = None
        self.generation += 1

    def _meaningful(self, s: FieldSnapshot) -> bool:
        prev = self._analyzed.get(s.field_id)
        return prev is None or _token_distance(prev, _tokens(s.text)) >= MEANINGFUL_TOKENS

    def _fire_settled(self, s: FieldSnapshot, gen: int) -> None:
        self._settled_pending = False
        self._on_settled(s, gen)

    def _fire_analyze(self, s: FieldSnapshot, gen: int) -> None:
        self._analyzed[s.field_id] = _tokens(s.text)
        self._on_analyze(s, gen)

    def _cancel_settled(self) -> None:
        if self._settled is not None:
            self._settled.cancel()
            self._settled = None
        self._settled_pending = False

    def _cancel_all(self) -> None:
        for h in (self._local, self._settled, self._analyze):
            if h is not None:
                h.cancel()
        self._local = self._settled = self._analyze = None
        self._settled_pending = False
