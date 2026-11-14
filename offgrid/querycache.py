"""A small, bounded, process-local cache for expensive local queries.

Nothing is written to disk and nothing is shared between processes. The cache
is intentionally explicit: callers choose the key and clear it when their
underlying data changes, so a stale answer cannot silently outlive a library
edit or a memory write.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Hashable, Optional


class QueryCache:
    """Least-recently-used cache with hit/miss counters."""

    def __init__(self, max_entries: int = 32) -> None:
        self.max_entries = max(1, int(max_entries))
        self._items: "OrderedDict[Hashable, Any]" = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: Hashable) -> Optional[Any]:
        with self._lock:
            if key not in self._items:
                self.misses += 1
                return None
            value = self._items.pop(key)
            self._items[key] = value
            self.hits += 1
            return value

    def put(self, key: Hashable, value: Any) -> None:
        with self._lock:
            self._items.pop(key, None)
            self._items[key] = value
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def stats(self) -> "dict[str, int]":
        with self._lock:
            return {
                "entries": len(self._items),
                "max_entries": self.max_entries,
                "hits": self.hits,
                "misses": self.misses,
            }
