"""Text-free logging: only scalars and short safe tokens ever reach stderr."""
from __future__ import annotations

import hashlib
import os
import re
import sys

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


def log_event(event: str, **fields: object) -> None:
    parts = [event] + [_fmt(k, v) for k, v in fields.items()]
    print(" ".join(parts), file=sys.stderr, flush=True)


def fingerprint(text: str) -> str:
    sha = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:8]
    return f"len={len(text)},sha={sha}"
