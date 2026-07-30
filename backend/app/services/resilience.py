"""
Resilience primitives (Step 8): retry-with-backoff + circuit breaker.

Azure ARM throttles with HTTP 429 (and occasionally 503). `retry_request` retries those with
exponential backoff, honouring a `Retry-After` header when present. A `CircuitBreaker` trips after
repeated failures so we fail fast instead of hammering a struggling dependency; it self-heals via a
half-open probe after a cooldown.

`_sleep` is module-level so tests can patch out the actual waiting.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Awaitable, Callable, Optional

import httpx

logger = logging.getLogger("cat.resilience")

_sleep = asyncio.sleep  # patched in tests
# 429 = throttled; 500/502/503/504 = transient server errors Azure (esp. Cost Management) throws under
# load. All are safe to retry with backoff — they self-heal within the run instead of losing data.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class CircuitOpenError(RuntimeError):
    """Raised when a call is short-circuited because the breaker is open."""


class CircuitBreaker:
    def __init__(self, name: str = "azure", fail_threshold: int = 5,
                 reset_timeout: float = 30.0, time_func: Callable[[], float] = time.monotonic):
        self.name = name
        self.fail_threshold = fail_threshold
        self.reset_timeout = reset_timeout
        self._now = time_func
        self._failures = 0
        self._opened_at: Optional[float] = None
        self.state = "closed"  # closed | open | half_open

    def allow(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open":
            if self._opened_at is not None and self._now() - self._opened_at >= self.reset_timeout:
                self.state = "half_open"  # allow a single probe
                return True
            return False
        return True  # half_open → allow the probe

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self.state = "closed"

    def record_failure(self) -> None:
        self._failures += 1
        if self.state == "half_open" or self._failures >= self.fail_threshold:
            self.state = "open"
            self._opened_at = self._now()
            logger.warning("Circuit '%s' opened after %d failures", self.name, self._failures)


def _retry_after_seconds(response: httpx.Response) -> Optional[float]:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


async def retry_request(
    send: Callable[[], Awaitable[httpx.Response]],
    *,
    max_retries: int = 4,
    base_delay: float = 0.5,
    breaker: Optional[CircuitBreaker] = None,
) -> httpx.Response:
    """Call `send()` with retry/backoff on throttling + a circuit breaker.

    Returns the final `httpx.Response` (the caller still decides via `raise_for_status`). Transport
    errors are retried and re-raised if they persist. When `breaker` is open, raises CircuitOpenError.
    """
    attempt = 0
    while True:
        if breaker is not None and not breaker.allow():
            raise CircuitOpenError(f"Circuit '{breaker.name}' is open; failing fast.")
        try:
            response = await send()
        except httpx.TransportError as exc:
            if breaker is not None:
                breaker.record_failure()
            if attempt >= max_retries:
                logger.warning("Transport error after %d retries: %s", attempt, exc)
                raise
            await _sleep(_backoff(base_delay, attempt))
            attempt += 1
            continue

        if response.status_code in RETRYABLE_STATUS and attempt < max_retries:
            delay = _retry_after_seconds(response) or _backoff(base_delay, attempt)
            logger.info("Throttled (%d); retrying in %.2fs (attempt %d)",
                        response.status_code, delay, attempt + 1)
            await _sleep(delay)
            attempt += 1
            continue

        if response.status_code in RETRYABLE_STATUS:
            # Retries exhausted while still throttled → count as a failure.
            if breaker is not None:
                breaker.record_failure()
            return response

        if breaker is not None:
            breaker.record_success()
        return response


def _backoff(base_delay: float, attempt: int) -> float:
    """Exponential backoff with full jitter."""
    return random.uniform(0, base_delay * (2 ** attempt))
