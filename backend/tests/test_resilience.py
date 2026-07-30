"""Tests for retry/backoff + circuit breaker (Step 8)."""
from __future__ import annotations

import httpx
import pytest

from app.services import resilience
from app.services.azure_client import AzureClient
from app.services.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    retry_request,
)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Make backoff instant."""
    async def _instant(_seconds):
        return None
    monkeypatch.setattr(resilience, "_sleep", _instant)


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def _responder(statuses):
    """An async `send` that yields the given status codes in order, then 200 forever."""
    seq = list(statuses)
    calls = {"n": 0}

    async def send():
        calls["n"] += 1
        code = seq.pop(0) if seq else 200
        return httpx.Response(code, json={"ok": code == 200})

    return send, calls


# ── retry_request ────────────────────────────────────────────────────────────────

async def test_retries_429_then_succeeds():
    send, calls = _responder([429, 429, 200])
    resp = await retry_request(send, max_retries=4, base_delay=0.01)
    assert resp.status_code == 200
    assert calls["n"] == 3


async def test_gives_up_after_max_retries_returns_last():
    send, calls = _responder([429, 429, 429, 429, 429, 429])
    resp = await retry_request(send, max_retries=2, base_delay=0.01)
    assert resp.status_code == 429
    assert calls["n"] == 3  # initial + 2 retries


async def test_respects_retry_after_header(monkeypatch):
    delays = []

    async def _capture(seconds):
        delays.append(seconds)

    monkeypatch.setattr(resilience, "_sleep", _capture)

    async def send():
        if not delays:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200)

    resp = await retry_request(send, max_retries=3, base_delay=0.01)
    assert resp.status_code == 200
    assert delays[0] == 7.0  # honoured the header


async def test_transport_error_retried_then_raised():
    calls = {"n": 0}

    async def send():
        calls["n"] += 1
        raise httpx.ConnectError("down", request=httpx.Request("GET", "http://x"))

    with pytest.raises(httpx.ConnectError):
        await retry_request(send, max_retries=2, base_delay=0.01)
    assert calls["n"] == 3


# ── CircuitBreaker ──────────────────────────────────────────────────────────────

def test_breaker_opens_after_threshold():
    cb = CircuitBreaker(fail_threshold=3)
    cb.record_failure()
    cb.record_failure()
    assert cb.allow() is True
    cb.record_failure()  # 3rd → open
    assert cb.state == "open"
    assert cb.allow() is False


def test_breaker_half_open_after_cooldown_then_closes():
    clock = FakeClock()
    cb = CircuitBreaker(fail_threshold=1, reset_timeout=30, time_func=clock)
    cb.record_failure()
    assert cb.allow() is False
    clock.advance(31)
    assert cb.allow() is True       # half-open probe allowed
    assert cb.state == "half_open"
    cb.record_success()
    assert cb.state == "closed"


async def test_retry_request_raises_when_circuit_open():
    cb = CircuitBreaker(fail_threshold=1, reset_timeout=999)
    cb.record_failure()  # open
    send, _ = _responder([200])
    with pytest.raises(CircuitOpenError):
        await retry_request(send, breaker=cb)


# ── AzureClient integration ──────────────────────────────────────────────────────

async def test_azure_client_retries_429():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"value": [{"subscriptionId": "sub-1", "state": "Enabled"}]})

    client = AzureClient("t", transport=httpx.MockTransport(handler), base_delay=0.01)
    subs = await client.get_subscriptions()
    assert subs[0]["subscriptionId"] == "sub-1"
    assert calls["n"] == 2  # retried once after the 429
