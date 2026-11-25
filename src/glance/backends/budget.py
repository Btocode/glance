"""Call budget: a sliding per-minute window and a per-day counter that survives restarts."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from collections import deque
from collections.abc import Callable
from datetime import date
from pathlib import Path

from glance.clock import Clock
from glance.config import BackendCfg

WINDOW_S = 60.0


class Budget:
    """Every AI call (automatic or clicked) spends one unit.

    The store holds only `{"date": "YYYY-MM-DD", "count": N}`. It is read once at start and
    written atomically on every spend. Anything that would let the daily cap be bypassed
    fails closed: an unreadable or malformed store counts as a fully used day, and a spend
    that cannot be persisted is refused.
    """

    def __init__(self, clock: Clock, cfg: Callable[[], BackendCfg], store: Path,
                 today: Callable[[], date] = date.today) -> None:
        self._clock, self._cfg, self._store, self._today = clock, cfg, store, today
        self._lock = threading.Lock()
        self._recent: deque[float] = deque()
        self._unsaved = False
        self._date = today().isoformat()
        self._count = self._load()

    # -- store ---------------------------------------------------------------
    def _load(self) -> int:
        try:
            raw = self._store.read_text(encoding="utf-8")
        except FileNotFoundError:
            return 0
        except (OSError, UnicodeDecodeError):
            return self._close_today()
        try:
            data = json.loads(raw)
            day, count = data["date"], data["count"]
            if type(day) is not str or type(count) is not int or count < 0:
                raise ValueError
            date.fromisoformat(day)
        except (ValueError, KeyError, TypeError):
            return self._close_today()
        return count if day == self._date else 0

    def _close_today(self) -> int:
        count = max(self._cfg().per_day, 0)
        self._unsaved = not self._save(count)
        return count

    def _save(self, count: int) -> bool:
        tmp = None
        try:
            self._store.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._store.parent, prefix=".usage-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:  # mkstemp creates it 0600
                json.dump({"date": self._date, "count": count}, f)
            os.replace(tmp, self._store)
            return True
        except OSError:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return False

    # -- state ---------------------------------------------------------------
    def _roll(self) -> None:
        day = self._today().isoformat()
        if day != self._date:
            self._date, self._count, self._unsaved = day, 0, False
        cutoff = self._clock.now() - WINDOW_S
        while self._recent and self._recent[0] <= cutoff:
            self._recent.popleft()

    def _state(self) -> str | None:
        cfg = self._cfg()
        if self._unsaved or self._count >= cfg.per_day:
            return "day"
        if len(self._recent) >= cfg.per_minute:
            return "minute"
        return None

    # -- public --------------------------------------------------------------
    def try_spend(self) -> bool:
        with self._lock:
            self._roll()
            self._unsaved = False  # a failed write is retried by the next spend
            if self._state() is not None:
                return False
            if not self._save(self._count + 1):
                self._unsaved = True
                return False
            self._count += 1
            self._recent.append(self._clock.now())
            return True

    def exhausted(self) -> str | None:
        with self._lock:
            self._roll()
            return self._state()

    def used_today(self) -> int:
        with self._lock:
            self._roll()
            return self._count
