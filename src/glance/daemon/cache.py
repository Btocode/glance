"""In-memory LRU for analysis results. Nothing here ever touches the disk."""
from __future__ import annotations

import hashlib
from collections import OrderedDict
from typing import Any


def cache_key(*parts: str) -> str:
    """sha256 over the parts (length-prefixed, so their boundaries count): never raw text."""
    h = hashlib.sha256()
    for part in parts:
        data = part.encode("utf-8", "surrogatepass")
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    return h.hexdigest()


class LruCache:
    """Least-recently-used map. Both `get` and `put` count as a use."""

    def __init__(self, max_entries: int = 128) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._max = max_entries
        self._items: OrderedDict[str, Any] = OrderedDict()

    def get(self, key: str) -> Any | None:
        if key not in self._items:
            return None
        self._items.move_to_end(key)
        return self._items[key]

    def put(self, key: str, value: Any) -> None:
        self._items[key] = value
        self._items.move_to_end(key)
        while len(self._items) > self._max:
            self._items.popitem(last=False)

    def clear(self) -> None:
        self._items.clear()
