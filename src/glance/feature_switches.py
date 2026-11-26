"""The feature switches: one registry the tray's "Features ▸" submenu, `glance features` and
the daemon all read. Pure (no GTK, no AppKit).

A feature is one config key with an "on" and an "off" value. Adding one is one `Feature(...)`
entry here: the menu and the CLI list it from then on, and turning it on or off writes that
key (keeping the user's file as it was), which every running glanced picks up within a
second (`ConfigWatcher`) or at once when it wrote the file itself.

Each key is read from its typed table (`[selection]` -> `SelectionCfg`, `[shot]` ->
`ShotCfg`, ...); a section without one would be read from `Config.raw`. glanced follows the
switches live: the selection watcher (`SelectionActions.sync`) and Glance Shot's hotkey and
tray item (`daemon.shot_wiring.ShotSwitch`) both ask `is_on`.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from glance.log import log_event

_MISSING = object()


@dataclass(frozen=True)
class Feature:
    name: str            # `glance features <name> on|off`
    label: str           # the tray's check item
    key: str             # the dotted config key it writes
    default: Any         # the key's value when the file does not set it
    on: Any = True       # the value written for "on" ...
    off: Any = False     # ... and for "off"
    menu: bool = True    # a check item in "Features ▸" (False: the menu shows it otherwise)
    # The state when the key is unset and `default` is not one of on/off (e.g. None: "auto").
    auto: Callable[[Any], bool] | None = None
    note: str = ""       # one line for `glance features`

    def value(self, cfg: Any) -> Any:
        section, _, leaf = self.key.partition(".")
        table = getattr(cfg, section, None)
        if table is not None and not isinstance(table, dict) and hasattr(table, leaf):
            v = getattr(table, leaf)
        else:
            raw = getattr(cfg, "raw", {}) or {}
            sect = raw.get(section) if isinstance(raw, dict) else None
            v = sect.get(leaf, _MISSING) if isinstance(sect, dict) else _MISSING
        return self.default if v is _MISSING else v

    def is_on(self, cfg: Any) -> bool:
        v = self.value(cfg)
        if v == self.on:
            return True
        if v == self.off:
            return False
        if self.auto is not None:
            return bool(self.auto(cfg))
        return bool(self.default == self.on)


def _autocomplete_auto(cfg: Any) -> bool:
    """`[backend] live_autocomplete` unset: live completion only with the api backend."""
    return getattr(getattr(cfg, "backend", None), "choice", None) == "api"


FEATURES: tuple[Feature, ...] = (
    # The tray shows this one as the two "Check: on shortcut / while typing" radio items.
    Feature("typing", "Check while typing", "general.check", "shortcut", "typing", "shortcut",
            menu=False, note="off: only on the check shortcut"),
    Feature("selection", "Selection actions", "selection.enabled", True,
            note="the Glance mark beside selected text (the shortcut works either way)"),
    Feature("shot", "Glance Shot", "shot.enabled", True,
            note="capture a screen region and act on it; tray: Take a Glance Shot"),
    Feature("autocomplete", "Autocomplete while typing", "backend.live_autocomplete", None,
            auto=_autocomplete_auto, note="unset: only with the api backend"),
    Feature("fluent", "Fluent corrections", "general.correction", "minimal",
            on="fluent", off="minimal",
            note="off: mistakes only; on: may also reword a sentence to make it grammatical"),
)
NAMES = tuple(f.name for f in FEATURES)


def feature(name: str) -> Feature:
    for f in FEATURES:
        if f.name == name:
            return f
    raise KeyError(name)


@dataclass(frozen=True)
class FeatureRow:
    name: str
    label: str
    on: bool


def menu_rows(cfg: Any) -> tuple[FeatureRow, ...]:
    """The "Features ▸" check items, in order."""
    return tuple(FeatureRow(f.name, f.label, f.is_on(cfg)) for f in FEATURES if f.menu)


def is_on(cfg: Any, name: str) -> bool:
    return feature(name).is_on(cfg)


def listing(cfg: Any) -> list[str]:
    """`glance features`: one line per feature."""
    width = max(len(n) for n in NAMES)
    out = []
    for f in FEATURES:
        state = "on" if f.is_on(cfg) else "off"
        out.append(f"{f.name:<{width}}  {state:<3}  {f.label} ({f.key}"
                   + (f"; {f.note}" if f.note else "") + ")")
    return out


def set_feature(update_config: Callable[[Path, str, Any], None], path: Path, name: str,
                on: bool, reload: Callable[[], None] = lambda: None) -> bool:
    """Write the feature's key (keeping the file's layout), then `reload()` so a running
    glanced applies it now. False (logged) when the config cannot be written."""
    from glance.config import ConfigError

    f = feature(name)
    try:
        update_config(path, f.key, f.on if on else f.off)
    except (ConfigError, OSError) as e:
        log_event("config_write_failed", kind=type(e).__name__)
        return False
    reload()
    return True
