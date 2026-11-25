"""Personal dictionary and ignored rules: the only files Glance keeps next to the user's text.

They hold words and rule names only, and are written atomically.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class PersonalDictionary:
    """One word per line; lookups are casefolded."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._words: list[str] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        seen: set[str] = set()
        for w in (ln.strip() for ln in lines):
            if w and w.casefold() not in seen:
                seen.add(w.casefold())
                self._words.append(w)

    def add(self, word: str) -> None:
        word = word.strip()
        if not word or any(c.isspace() for c in word):
            raise ValueError("a dictionary entry is a single word")
        if self.contains(word):
            return
        self._words.append(word)
        _atomic_write(self.path, "".join(w + "\n" for w in self._words))

    def words(self) -> frozenset[str]:
        return frozenset(self._words)

    def contains(self, word: str) -> bool:
        folded = word.casefold()
        return any(w.casefold() == folded for w in self._words)


class IgnoredRules:
    """Rules the user switched off, globally or per app (keyed by casefolded WM_CLASS)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._global: set[str] = set()
        self._apps: dict[str, set[str]] = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._global = {str(r) for r in data.get("global", [])}
            self._apps = {str(k).casefold(): {str(r) for r in v}
                          for k, v in data.get("apps", {}).items()}
        except (OSError, ValueError, AttributeError, TypeError):
            self._global, self._apps = set(), {}

    def ignore(self, rule: str, wm_class: str | None) -> None:
        if wm_class is None:
            self._global.add(rule)
        else:
            self._apps.setdefault(wm_class.casefold(), set()).add(rule)
        doc = {"global": sorted(self._global),
               "apps": {k: sorted(v) for k, v in sorted(self._apps.items())}}
        _atomic_write(self.path, json.dumps(doc, indent=2) + "\n")

    def is_ignored(self, rule: str, wm_class: str | None) -> bool:
        if rule in self._global:
            return True
        return wm_class is not None and rule in self._apps.get(wm_class.casefold(), ())

    def global_rules(self) -> set[str]:
        return set(self._global)
