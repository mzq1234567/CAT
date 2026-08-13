"""
Azure API reliability tests (Batch 2): transient-error retries + stats, bounded backoff, GLOBAL
concurrency limiting, complete pagination, and the data-quality/completeness model.

These complement test_resilience.py (429/Retry-After/circuit-breaker) and test_resource_graph.py
(ARG $skipToken paging).
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.services import resilience
from app.services.azure_client import AzureClient
from app.services.collection import COMPLETE, FAILED, PARTIAL, CollectionReport, RetryStats
from app.services.resilience import MAX_BACKOFF_SECONDS, _backoff, retry_request


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _instant(_seconds):
        return None
    monkeypatch.setattr(resilience, "_sleep", _instant)


def _responder(statuses):
    seq = list(statuses)
    calls = {"n": 0}

    async def send():
        calls["n"] += 1
        code = seq.pop(0) if seq else 200
        return httpx.Response(code, json={"ok": code == 200})

    return send, calls


# ── Transient 5xx + retry stats ──────────────────────────────────────────────────────

async def test_retries_transient_500_then_succeeds():
    send, calls = _responder([500, 503, 200])
    stats = RetryStats()
    resp = await retry_request(send, max_retries=4, base_delay=0.01, stats=stats)
    assert resp.status_code == 200 and calls["n"] == 3
    assert stats.server_errors == 2 and stats.retries == 2 and stats.exhausted == 0


async def test_retry_stats_count_throttles_and_exhaustion():
    send, _ = _responder([429, 429, 429, 429])
    stats = RetryStats()
    resp = await retry_request(send, max_retries=2, base_delay=0.01, stats=stats)
    assert resp.status_code == 429
    assert stats.throttled_responses == 3       # 429 seen on the 3 attempts made
    assert stats.retries == 2                    # bounded by max_retries
    assert stats.exhausted == 1                  # gave up still-throttled (never silently "ok")


async def test_transport_error_counts_and_reraises():
    calls = {"n": 0}

    async def send():
        calls["n"] += 1
        raise httpx.ConnectError("down", request=httpx.Request("GET", "http://x"))

    stats = RetryStats()
    with pytest.raises(httpx.ConnectError):
        await retry_request(send, max_retries=2, base_delay=0.01, stats=stats)
    assert stats.transport_errors == 3 and calls["n"] == 3


# ── Bounded backoff ────────────────────────────────────────────────────────────────

def test_backoff_is_bounded_by_cap():
    # Even at a high attempt count the (jittered) backoff never exceeds the cap.
    for attempt in range(0, 12):
        assert 0.0 <= _backoff(0.5, attempt) <= MAX_BACKOFF_SECONDS


async def test_retry_after_capped_at_max_backoff(monkeypatch):
    delays = []

    async def _capture(seconds):
        delays.append(seconds)
    monkeypatch.setattr(resilience, "_sleep", _capture)

    async def send():
        if not delays:
            return httpx.Response(429, headers={"Retry-After": "9999"})  # absurd server value
        return httpx.Response(200)

    resp = await retry_request(send, max_retries=3, base_delay=0.01)
    assert resp.status_code == 200
    assert delays[0] == MAX_BACKOFF_SECONDS       # clamped, never a multi-hour stall


# ── GLOBAL concurrency limit ─────────────────────────────────────────────────────────

class _ConcurrencyTrackingTransport(httpx.AsyncBaseTransport):
    """Records the maximum number of simultaneously in-flight requests."""
    def __init__(self):
        self.in_flight = 0
        self.max_in_flight = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.01)   # hold the slot so overlap is observable
            return httpx.Response(200, json={"value": []})
        finally:
            self.in_flight -= 1


async def test_global_concurrency_limit_is_respected():
    transport = _ConcurrencyTrackingTransport()
    client = AzureClient("t", transport=transport, max_concurrency=3, base_delay=0.001)
    # Fire many more calls than the limit; the shared semaphore must cap in-flight requests.
    await asyncio.gather(*(client.get_subscriptions() for _ in range(20)))
    assert transport.max_in_flight <= 3
    assert transport.max_in_flight >= 2   # and it IS parallelising (not accidentally serial)


# ── Pagination completeness ──────────────────────────────────────────────────────────

async def test_cost_management_follows_nextlink_across_pages():
    pages = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        pages["n"] += 1
        if pages["n"] == 1:
            return httpx.Response(200, json={"properties": {
                "columns": [{"name": "Cost"}, {"name": "ResourceId"}],
                "rows": [[10.0, "/r/a"]], "nextLink": "https://management.azure.com/next-page"}})
        return httpx.Response(200, json={"properties": {
            "columns": [{"name": "Cost"}, {"name": "ResourceId"}], "rows": [[20.0, "/r/b"]]}})

    client = AzureClient("t", transport=httpx.MockTransport(handler), base_delay=0.001)
    result = await client.query_cost_management("sub-1", {"type": "ActualCost"})
    rows = result["properties"]["rows"]
    assert len(rows) == 2 and pages["n"] == 2      # BOTH pages followed, not just the first


async def test_cost_management_single_and_empty_pages():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"properties": {"columns": [], "rows": []}})

    client = AzureClient("t", transport=httpx.MockTransport(handler), base_delay=0.001)
    result = await client.query_cost_management("sub-1", {"type": "ActualCost"})
    assert result["properties"]["rows"] == []


async def test_advisor_follows_nextlink_across_pages():
    pages = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        pages["n"] += 1
        if pages["n"] == 1:
            return httpx.Response(200, json={"value": [{"id": "adv-1"}],
                                             "nextLink": "https://management.azure.com/advisor-next"})
        return httpx.Response(200, json={"value": [{"id": "adv-2"}]})

    client = AzureClient("t", transport=httpx.MockTransport(handler), base_delay=0.001)
    recs = await client.get_advisor_cost_recommendations("sub-1")
    assert {r["id"] for r in recs} == {"adv-1", "adv-2"} and pages["n"] == 2


async def test_resource_graph_follows_skiptoken_over_many_pages():
    total_pages = 4

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        body = _json.loads(request.content.decode())
        page = int((body.get("options", {}) or {}).get("$skipToken") or 1)
        rows = [{"id": f"/r/{page}-{i}"} for i in range(1000)]   # a full 1000-row page
        payload = {"data": rows}
        if page < total_pages:
            payload["$skipToken"] = str(page + 1)
        return httpx.Response(200, json=payload)

    client = AzureClient("t", transport=httpx.MockTransport(handler), base_delay=0.001)
    rows = await client.query_resource_graph(["sub-1"], "Resources | project id")
    assert len(rows) == total_pages * 1000        # every page retrieved — the FULL result set


async def test_resource_graph_empty_result():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    client = AzureClient("t", transport=httpx.MockTransport(handler), base_delay=0.001)
    assert await client.query_resource_graph(["sub-1"], "Resources") == []


# ── Data-quality / completeness model ────────────────────────────────────────────────

def test_completeness_complete_when_nothing_failed():
    r = CollectionReport(subscriptions_requested=1, resources_discovered=42)
    r.note_inventory(buckets_total=20, failed_buckets=[])
    assert r.data_quality() == COMPLETE
    assert r.client_message() is None


def test_completeness_partial_on_any_failure():
    r = CollectionReport(subscriptions_requested=1, resources_discovered=42)
    r.note_inventory(buckets_total=20, failed_buckets=["orphaned_public_ips"])
    assert r.data_quality() == PARTIAL
    msg = r.client_message()
    assert msg and "excluded from quantified savings" in msg
    # No developer terminology / HTTP codes in the client line.
    assert "429" not in msg and "Traceback" not in msg


def test_completeness_partial_on_metrics_or_billing_failure():
    r = CollectionReport(subscriptions_requested=1, resources_discovered=10)
    r.note_inventory(20, [])
    r.note_metrics(requested=5, failed=2)
    assert r.data_quality() == PARTIAL
    r2 = CollectionReport(subscriptions_requested=1, resources_discovered=10)
    r2.note_inventory(20, [])
    r2.billing_failed_subs = 1
    assert r2.data_quality() == PARTIAL


def test_completeness_failed_when_all_inventory_failed():
    r = CollectionReport(subscriptions_requested=1)
    r.note_inventory(buckets_total=20, failed_buckets=[f"b{i}" for i in range(20)])
    assert r.data_quality() == FAILED
    assert "could not be collected" in (r.client_message() or "")


def test_retry_exhaustion_flags_partial():
    r = CollectionReport(subscriptions_requested=1, resources_discovered=10)
    r.note_inventory(20, [])
    r.retry.exhausted = 1
    assert r.data_quality() == PARTIAL
