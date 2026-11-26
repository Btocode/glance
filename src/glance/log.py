"""Text-free logging: only scalars and short safe tokens ever reach stderr."""
from __future__ import annotations

import hashlib
import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager

_SAFE = re.compile(r"^[A-Za-z0-9_.:/+-]{0,64}$")


class UnsafeLogValue(ValueError):
    pass


def _fmt(key: str, value: object) -> str:
    if value is None or isinstance(value, (bool, int, float)):
        return f"{key}={value}"
    if isinstance(value, str) and _SAFE.match(value):
        return f"{key}={value}"
    if os.environ.get("GLANCE_STRICT_LOG") == "1":
        raise UnsafeLogValue(key)
    return f"{key}=<redacted>"


_quiet = False


@contextmanager
def quiet() -> Iterator[None]:
    """No log lines while this is open: the `glance` command line wraps itself in it.

    Log lines belong to glanced (the journal on Linux, ~/Library/Logs/Glance on macOS) and to
    its children, which write them to stderr. A command run in a terminal must not: its stderr
    is the user's screen. GLANCE_DEBUG_LOG=1 shows them anyway."""
    global _quiet
    before = _quiet
    _quiet = os.environ.get("GLANCE_DEBUG_LOG") != "1"
    try:
        yield
    finally:
        _quiet = before


def log_event(event: str, **fields: object) -> None:
    if _quiet:
        return
    parts = [event] + [_fmt(k, v) for k, v in fields.items()]
    print(" ".join(parts), file=sys.stderr, flush=True)


def fingerprint(text: str) -> str:
    sha = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:8]
    return f"len={len(text)},sha={sha}"
