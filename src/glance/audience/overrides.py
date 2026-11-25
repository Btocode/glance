"""Remembered per-context profile choices: `{context key: profile name}`, nothing else."""
from __future__ import annotations

import json
import os
from pathlib import Path


class Overrides:
    def __init__(self, path: Path) -> None:
        self._path = path

    def _load(self) -> dict[str, str]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}

    def _save(self, data: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self._path)

    def get(self, key: str) -> str | None:
        return self._load().get(key)

    def set(self, key: str, profile: str) -> None:
        data = self._load()
        data[key] = profile
        self._save(data)

    def clear(self, key: str) -> None:
        data = self._load()
        if data.pop(key, None) is not None:
            self._save(data)
