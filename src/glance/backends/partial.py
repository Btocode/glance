"""Read one string value out of a JSON object that is still streaming in.

The analyze answer is a JSON object whose first key is "corrected" (the draft with its
mistakes fixed). While it streams, the part of that string received so far can be shown;
nothing here guesses past what has arrived.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r",
            "t": "\t"}


def _hex4(digits: str) -> int:
    try:
        return int(digits, 16)
    except ValueError:
        return -1


def partial_string(text: str, key: str) -> tuple[str, bool] | None:
    """(the value of `key` as far as it has arrived, whether its closing quote has), or None
    while the key or the value's opening quote is not there yet. A half-received escape
    sequence at the end is left out."""
    m = re.search(r'"' + re.escape(key) + r'"\s*:\s*"', text)
    if m is None:
        return None
    out: list[str] = []
    i, n = m.end(), len(text)
    while i < n:
        c = text[i]
        if c == '"':
            return "".join(out), True
        if c != "\\":
            out.append(c)
            i += 1
            continue
        if i + 1 >= n:
            break
        e = text[i + 1]
        if e == "u":
            hexa = text[i + 2:i + 6]
            if len(hexa) < 4:
                break
            try:
                code = int(hexa, 16)
            except ValueError:
                return "".join(out), False
            if 0xD800 <= code < 0xDC00:  # a surrogate pair: wait for its second half
                low = text[i + 6:i + 12]
                if len(low) < 6:
                    break
                lo = _hex4(low[2:]) if low[:2] == "\\u" else -1
                if 0xDC00 <= lo < 0xE000:
                    out.append(chr(0x10000 + ((code - 0xD800) << 10) + (lo - 0xDC00)))
                    i += 12
                    continue
            out.append(chr(code))
            i += 6
            continue
        out.append(_ESCAPES.get(e, e))
        i += 2
    return "".join(out), False


# Where an `analyze` reports its corrected text as it streams in (see `analyze_progress`).
_on_partial: ContextVar[Callable[[str], None] | None] = ContextVar("glance_analyze_partial",
                                                                   default=None)


@contextmanager
def analyze_progress(on_partial: Callable[[str], None] | None) -> Iterator[None]:
    """`analyze` calls made inside (on this thread) report the "corrected" text received so
    far to `on_partial` (on the calling thread), where the backend streams."""
    token = _on_partial.set(on_partial)
    try:
        yield
    finally:
        _on_partial.reset(token)


class CorrectedWatcher:
    """Feed it the answer's text as it grows; it reports each longer "corrected" prefix."""

    def __init__(self, key: str = "corrected") -> None:
        self._key = key
        self._cb = _on_partial.get()
        self._sent = ""
        self._done = False

    def feed(self, text: str) -> None:
        if self._cb is None or self._done:
            return
        got = partial_string(text, self._key)
        if got is None:
            return
        value, self._done = got
        if len(value) > len(self._sent):
            self._sent = value
            try:
                self._cb(value)
            except Exception:  # noqa: BLE001 - a display hook must not fail the call
                self._cb = None
