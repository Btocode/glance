"""Which AI glanced uses when the user has not chosen one, or the chosen one is gone.

The first connected backend in ORDER, but never an experimental one while a non-experimental
one is connected. glanced writes `[backend] choice` only when the user had none (and leaves a
marker beside the config, so `glance doctor` can say it was chosen automatically); a choice
that stopped working is left in the config and only stood in for at run time.
"""
from __future__ import annotations

from collections.abc import Container, Iterable, Mapping
from pathlib import Path

from glance.backends.base import Health

ORDER = ("claude", "codex", "gemini", "opencode", "api")
# codex and opencode report "experimental: ..." in their health when signed in.
EXPERIMENTAL = frozenset({"codex", "opencode"})
MARKER = "backend-chosen-automatically"


def notice(name: str) -> tuple[str, str]:
    return ("Glance", f"Using {name} for AI suggestions — change with `glance backend use`")


def ready(h: Health | None) -> bool:
    """Connected for certain: installed and signed in."""
    return h is not None and h.installed and h.signed_in is True


def usable(h: Health | None) -> bool:
    """Not known to be broken: installed and not signed out (unknown / timed out counts)."""
    return h is not None and h.installed and h.signed_in is not False


def experimental(name: str, h: Health | None = None) -> bool:
    return name in EXPERIMENTAL or (h is not None and "experimental" in h.detail)


def pick(connected: Iterable[str], experimental_names: Container[str] = EXPERIMENTAL
         ) -> str | None:
    """The backend to use among `connected`, or None when there is none."""
    names = [n for n in ORDER if n in set(connected)]
    stable = [n for n in names if n not in experimental_names]
    return next(iter(stable or names), None)


def pick_from(healths: Mapping[str, Health]) -> str | None:
    connected = [n for n, h in healths.items() if ready(h)]
    return pick(connected, {n for n, h in healths.items() if experimental(n, h)})


def marker_path(config_file: Path) -> Path:
    return config_file.parent / MARKER


def read_marker(config_file: Path) -> str | None:
    try:
        name = marker_path(config_file).read_text().strip()
    except OSError:
        return None
    return name if name in ORDER else None


def write_marker(config_file: Path, name: str | None) -> None:
    """Record `name` as chosen automatically; None: the user chose (the marker goes)."""
    path = marker_path(config_file)
    try:
        if name is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name + "\n")
    except OSError:
        pass
