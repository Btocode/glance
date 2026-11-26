"""Glance's version: one source, `version` in pyproject.toml.

A checkout or editable install reads pyproject.toml next to the package (so a `git pull`
shows the new number at once); an installed wheel reads its own metadata."""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

UNKNOWN = "0+unknown"
_PROJECT = re.compile(r'^\[project\]\s*$(.*?)(?=^\[|\Z)', re.MULTILINE | re.DOTALL)
_FIELD = re.compile(r'^(name|version)\s*=\s*"([^"]+)"\s*$', re.MULTILINE)


def from_pyproject(path: Path) -> str | None:
    """`version` of the [project] table, if that table names Glance."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    m = _PROJECT.search(text)
    fields = dict(_FIELD.findall(m.group(1))) if m else {}
    return fields.get("version") if fields.get("name") == "glance" else None


@lru_cache(maxsize=1)
def version() -> str:
    found = from_pyproject(Path(__file__).resolve().parents[2] / "pyproject.toml")
    if found:
        return found
    try:
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as dist_version
        return dist_version("glance")
    except (ImportError, PackageNotFoundError):
        return UNKNOWN


def parse(v: str) -> tuple[tuple[int, ...], int, str]:
    """A sort key for "0.4.0", "v0.5.0", "0.5.0rc1": (numbers, final?, suffix).
    A pre-release sorts before its final release."""
    v = v.strip().lstrip("vV")
    m = re.match(r"(\d+(?:\.\d+)*)(.*)", v)
    if not m:
        return ((), 0, v)
    nums = tuple(int(x) for x in m.group(1).split("."))
    nums = nums + (0,) * (3 - len(nums)) if len(nums) < 3 else nums
    rest = m.group(2).lstrip(".-+")
    return (nums, 0 if rest else 1, rest)


def is_newer(candidate: str, current: str) -> bool:
    return parse(candidate) > parse(current)


__version__ = version()
