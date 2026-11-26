"""XDG locations used by Glance."""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    d = Path(base) / "glance"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    d.chmod(0o700)
    return d


def capture_sock() -> Path:
    return runtime_dir() / "capture.sock"


def ctl_sock() -> Path:
    return runtime_dir() / "ctl.sock"


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "glance" / "config.toml"


def data_dir(environ: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    """Glance's data dir; `environ`/`home` let doctor resolve it for a given (fake) home."""
    env = os.environ if environ is None else environ
    base = env.get("XDG_DATA_HOME") or str((home or Path.home()) / ".local" / "share")
    return Path(base) / "glance"


def state_dir(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    return Path(env.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")) / "glance"


def update_log_path() -> Path:
    return state_dir() / "update.log"


def dictionary_path() -> Path:
    return data_dir() / "dictionary.txt"


def ignored_path() -> Path:
    return data_dir() / "ignored.json"


def overrides_path() -> Path:
    return data_dir() / "overrides.json"


def overrides_manifest(environ: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    return data_dir(environ, home) / "desktop-overrides.json"


def usage_path() -> Path:
    return data_dir() / "usage.json"


def usage_ledger_path() -> Path:
    return data_dir() / "usage-ledger.json"


def harper_bin() -> Path:
    return data_dir() / "bin" / "harper-ls"
