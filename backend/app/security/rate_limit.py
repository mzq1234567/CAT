"""
Per-user / per-tenant rate limiting (Step 7).

Sliding-window limiter, in-memory + thread-safe. Keyed by tenant+user so one noisy user can't
exhaust the service for their whole tenant, and one tenant can't starve others. The state store is
deliberately simple so it can be swapped for Redis later (same as the pricing cache).
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List

from fastapi import Depends, HTTPException

from ..api.dependencies import get_current_user
from ..config import settings


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: float,
                 time_func: Callable[[], float] = time.monotonic):
        self.max_requests = max_requests
        self.window = window_seconds
        self._now = time_func
        self._hits: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """Record an attempt; return True if within the limit, False if it should be rejected."""
        with self._lock:
            now = self._now()
            window_start = now - self.window
            hits = [t for t in self._hits.get(key, []) if t >= window_start]
            if len(hits) >= self.max_requests:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            self._maybe_evict(window_start)
            return True

    def _maybe_evict(self, window_start: float) -> None:
        """Bound memory: periodically drop keys whose hits have all aged out of the window.

        Called under the lock. Cheap amortised cost — only sweeps once the map grows past a
        threshold, so a large fleet of one-off users can't leak memory indefinitely.
        """
        if len(self._hits) < 1024:
            return
        stale = [k for k, ts in self._hits.items() if not ts or ts[-1] < window_start]
        for k in stale:
            del self._hits[k]

    def retry_after(self, key: str) -> int:
        with self._lock:
            hits = self._hits.get(key, [])
            if not hits:
                return 0
            return max(0, int(self.window - (self._now() - hits[0])) + 1)


# Shared limiter for assessment creation (the expensive operation).
assessment_limiter = RateLimiter(
    max_requests=settings.rate_limit_max_requests,
    window_seconds=settings.rate_limit_window_seconds,
)


def enforce_assessment_rate_limit(user: dict = Depends(get_current_user)) -> dict:
    """FastAPI dependency: 429 when a user exceeds the assessment creation limit."""
    key = f"{user.get('tenant_id', 'unknown')}:{user.get('user_id', 'unknown')}"
    if not assessment_limiter.allow(key):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Please wait before starting another assessment.",
            headers={"Retry-After": str(assessment_limiter.retry_after(key))},
        )
    return user
