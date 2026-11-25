"""TypingWatch: when the user last changed the focused field's text, as capture sees it.

A write-back that starts while the user is typing interleaves with their keys: Glance moves
the caret, their next key lands there (S4 matrix, apply-while-typing). XInput2 and XRecord
would see every key but are not used; the text the field reports is enough. Every snapshot
capture takes is shown to the watch, and a text that differs from the previous one in the
same field is the user typing, unless it is a state the engine said its own write passes
through (`expect`).
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterable

from glance.protocol import text_sha256

QUIET_S = 0.4      # no apply starts sooner than this after the user's last change
OWN_TTL_S = 2.0    # how long a state the engine announced is taken for its own


class TypingWatch:
    """`fold` maps a text to the form in which two texts count as the same: what the
    application does to typed text on its own (no-break spaces, quotes) is not typing."""

    def __init__(self, now: Callable[[], float] = time.monotonic,
                 fold: Callable[[str], str] = lambda text: text) -> None:
        self._now = now
        self._fold = fold
        self._last: tuple[str, str] | None = None   # (field id, text hash) last observed
        self._own: dict[str, float] = {}            # text hash -> until when it is ours
        self._typed_at = float("-inf")

    def expect(self, texts: Iterable[str]) -> None:
        """The engine is about to produce these texts: they are not the user typing."""
        now = self._now()
        self._own = {h: t for h, t in self._own.items() if t > now}
        for text in texts:
            self._own[text_sha256(self._fold(text))] = now + OWN_TTL_S

    def observe(self, field_id: str, text: str) -> None:
        """A snapshot of the focused field (capture's main loop)."""
        sha = text_sha256(self._fold(text))
        prev, self._last = self._last, (field_id, sha)
        if prev is None or prev[0] != field_id or prev[1] == sha:
            return  # a new field, or only the caret moved
        if self._own.get(sha, float("-inf")) > self._now():
            return
        self._typed_at = self._now()

    def quiet_s(self) -> float:
        """Seconds since the user last changed the text (inf if never seen)."""
        return self._now() - self._typed_at
