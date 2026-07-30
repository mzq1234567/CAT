"""
Swappable cache abstraction.

Ships an in-memory TTL cache. The `CacheBackend` protocol is intentionally small so a
Redis-backed implementation can be dropped in later without touching call sites
(see Pending/Deferred in memory.md).

Key design point for resilience: `get_stale()` returns the last value written for a key
*regardless of TTL*. Callers (e.g. the pricing engine) use this to serve last-known-good
data when an upstream API is unavailable.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple


class CacheBackend(Protocol):
    def get(self, key: str) -> Optional[Any]: ...
    def set(self, key: str, value: Any, ttl_seconds: int) -> None: ...
    def get_stale(self, key: str) -> Optional[Any]: ...
    def keys(self) -> List[str]: ...
    def clear(self) -> None: ...


class InMemoryTTLCache:
    """Thread-safe in-memory cache with per-key TTL and last-known-good retention.

    Expired entries are *not* evicted eagerly — they are retained so `get_stale()` can
    return them as a fallback. This trades memory for resilience, which is fine for the
    bounded set of pricing keys we cache. A Redis backend would use native TTLs plus a
    separate `:lkg` key for the stale copy.
    """

    def __init__(self, time_func: Callable[[], float] = time.monotonic) -> None:
        # key -> (expires_at, value)
        self._store: Dict[str, Tuple[float, Any]] = {}
        self._now = time_func
        self._lock = threading.RLock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if self._now() >= expires_at:
                return None
            return value

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        with self._lock:
            self._store[key] = (self._now() + ttl_seconds, value)

    def get_stale(self, key: str) -> Optional[Any]:
        """Return the last-known value for a key even if its TTL has expired."""
        with self._lock:
            entry = self._store.get(key)
            return entry[1] if entry is not None else None

    def keys(self) -> List[str]:
        with self._lock:
            return list(self._store.keys())

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
