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

from .collection import RetryStats

logger = logging.getLogger("cat.resilience")

_sleep = asyncio.sleep  # patched in tests
# 429 = throttled; 500/502/503/504 = transient server errors Azure (esp. Cost Management) throws under
# load. All are safe to retry with backoff — they self-heal within the run instead of losing data.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# Upper bound on a single backoff wait, so an aggressive Retry-After or a high attempt count can never
# stall the run indefinitely (the retry count is already bounded; this bounds each wait's duration).
MAX_BACKOFF_SECONDS = 60.0


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
    label: str = "azure call",
    stats: Optional[RetryStats] = None,
    retry_after_cap: Optional[float] = None,
) -> httpx.Response:
    """Call `send()` with bounded retry/backoff on throttling + a circuit breaker.

    Returns the final `httpx.Response` (the caller still decides via `raise_for_status`). Transport
    errors are retried and re-raised if they persist. When `breaker` is open, raises CircuitOpenError.

    Retries are BOUNDED by `max_retries`; each wait is bounded by MAX_BACKOFF_SECONDS. Retryable
    responses are 429 (throttled) and transient 5xx, honouring `Retry-After` when Azure supplies it,
    else exponential backoff with full jitter. `label` names the API/resource for logs (never a token
    or secret — headers are not logged). `stats` accrues run-level throttle/retry counters.

    `retry_after_cap` bounds how long a single Azure-supplied `Retry-After` wait may be honoured. It
    defaults to the global MAX_BACKOFF_SECONDS; the Cost Management path passes a HIGHER cap (Azure's
    billing API throttles hard and asks for long waits, so honouring its `Retry-After` beyond 60s is
    what lets the query succeed instead of exhausting). It only affects the `Retry-After` branch — the
    exponential-backoff fallback stays capped at MAX_BACKOFF_SECONDS for every caller.
    """
    ra_cap = retry_after_cap if retry_after_cap is not None else MAX_BACKOFF_SECONDS
    attempt = 0
    while True:
        if breaker is not None and not breaker.allow():
            raise CircuitOpenError(f"Circuit '{breaker.name}' is open; failing fast for {label}.")
        try:
            response = await send()
        except httpx.TransportError as exc:
            if stats is not None:
                stats.transport_errors += 1
            if breaker is not None:
                breaker.record_failure()
            if attempt >= max_retries:
                logger.warning("%s: transport error after %d retries: %s",
                               label, attempt, type(exc).__name__)
                raise
            delay = _backoff(base_delay, attempt)
            if stats is not None:
                stats.retries += 1
            logger.info("%s: transport error (%s); retry %d/%d in %.2fs",
                        label, type(exc).__name__, attempt + 1, max_retries, delay)
            await _sleep(delay)
            attempt += 1
            continue

        status = response.status_code
        if status in RETRYABLE_STATUS and stats is not None:
            if status == 429:
                stats.throttled_responses += 1
            else:
                stats.server_errors += 1

        if status in RETRYABLE_STATUS and attempt < max_retries:
            retry_after = _retry_after_seconds(response)
            delay = min(retry_after, ra_cap) if retry_after is not None else _backoff(base_delay, attempt)
            reason = "throttled (429)" if status == 429 else f"server error ({status})"
            if stats is not None:
                stats.retries += 1
            logger.info("%s: %s; retry %d/%d in %.2fs%s",
                        label, reason, attempt + 1, max_retries, delay,
                        " (Retry-After)" if retry_after is not None else "")
            await _sleep(delay)
            attempt += 1
            continue

        if status in RETRYABLE_STATUS:
            # Retries exhausted while still throttled/erroring → count as a failure (never silently ok).
            if stats is not None:
                stats.exhausted += 1
            if breaker is not None:
                breaker.record_failure()
            logger.warning("%s: still %d after %d retries — giving up (data may be incomplete).",
                           label, status, max_retries)
            return response

        if breaker is not None:
            breaker.record_success()
        return response


def _backoff(base_delay: float, attempt: int) -> float:
    """Exponential backoff with full jitter, capped at MAX_BACKOFF_SECONDS."""
    return random.uniform(0, min(base_delay * (2 ** attempt), MAX_BACKOFF_SECONDS))
