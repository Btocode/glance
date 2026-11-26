"""The Rust twin of the privacy functions, from `glance_core` >= 0.2 (migration Phase 3).

`core` is the extension module when it is installed and has the privacy functions, else None
(an older glance_core, none at all, or GLANCE_PURE_PYTHON=1): then the Python reference
implementations run. Both give identical results (tests/test_privacy_parity.py). A text the
extension cannot take (a lone surrogate is not UTF-8) raises UnicodeEncodeError there; the
callers then use the Python implementation for that text.
"""
from __future__ import annotations

import os
from types import ModuleType

PURE_ENV = "GLANCE_PURE_PYTHON"


def load(environ: dict[str, str] | None = None) -> ModuleType | None:
    env = os.environ if environ is None else environ
    if env.get(PURE_ENV, "").strip() not in ("", "0"):
        return None
    try:
        import glance_core
    except ImportError:
        return None
    return glance_core if hasattr(glance_core, "privacy_find_secrets") else None


core = load()
