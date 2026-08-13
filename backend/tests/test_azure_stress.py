"""
Deterministic throttling STRESS test for the whole collection pipeline (Batch 2 hardening).

Drives the real `run_assessment` against a large mocked environment (120 VMs across 3 Resource Graph
pages, full metrics/billing/pricing) through a custom async transport that:
  * tracks the maximum simultaneously in-flight requests (concurrency),
  * injects 429 (Retry-After) + transient 5xx that succeed on retry,
  * makes ONE inventory bucket fail permanently (after retries).

It asserts the nine properties the hardening brief requires, and (separately) that multiple simultaneous
assessments respect the PROCESS-WIDE concurrency budget.
"""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import Base
from app.models.db import Assessment, Finding
from app.services import assessment as pipeline
from app.services import resilience
from app.services.azure_client import AzureClient
from app.services.pricing import PricingEngine
from tests.azure_mocks import retail_prices_handler

N_VMS = 120
PAGE = 40
VM_COST = 50.0   # each VM's grounded monthly billed cost


def _vm_rows():
    return [{
        "id": f"/subscriptions/sub-1/resourceGroups/rg/providers/microsoft.compute/virtualmachines/vm-{i}",
        "name": f"vm-{i}", "subscriptionId": "sub-1", "resourceGroup": "rg", "location": "eastus",
        "vmSize": "Standard_D2s_v3", "osType": "Linux", "powerState": "VM running", "tags": {},
    } for i in range(N_VMS)]


class StressTransport(httpx.AsyncBaseTransport):
    """Routes a full assessment's requests; tracks concurrency; injects recoverable + permanent faults."""
    def __init__(self):
        self.in_flight = 0
        self.max_in_flight = 0
        self.attempts: dict = defaultdict(int)
        self.vms = _vm_rows()
        self.retail = retail_prices_handler()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.002)   # hold the slot so overlap is observable
            return self._route(request)
        finally:
            self.in_flight -= 1

    def _inject(self, key: str, code: int):
        """Return a transient error response the FIRST time `key` is seen (recoverable on retry)."""
        self.attempts[key] += 1
        if self.attempts[key] == 1:
            headers = {"Retry-After": "0"} if code == 429 else {}
            return httpx.Response(code, headers=headers, json={"error": {"message": "transient"}})
        return None

    def _route(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "prices.azure.com":
            return self.retail(request)
        if path.endswith("/subscriptions"):
            return httpx.Response(200, json={"value": [{"subscriptionId": "sub-1", "state": "Enabled"}]})

        if "/providers/Microsoft.ResourceGraph/resources" in path:
            body = json.loads(request.content.decode())
            query = body["query"]
            if "snapshots" in query:                      # ONE bucket fails permanently (always 5xx)
                return httpx.Response(503, json={"error": {"message": "permanent"}})
            if "summarize" in query:
                return httpx.Response(200, json={"data": [
                    {"type": "microsoft.compute/virtualmachines", "resourceCount": N_VMS}]})
            if "'VM running'" in query:
                token = (body.get("options", {}) or {}).get("$skipToken")
                if token is None:                          # 429 on the first page, recovers on retry
                    injected = self._inject("argvm", 429)
                    if injected is not None:
                        return injected
                page = int(token or 1)
                start = (page - 1) * PAGE
                chunk = self.vms[start:start + PAGE]
                payload = {"data": chunk}
                if start + PAGE < N_VMS:
                    payload["$skipToken"] = str(page + 1)
                return httpx.Response(200, json=payload)
            return httpx.Response(200, json={"data": []})   # other buckets: genuinely empty

        if "/providers/microsoft.insights/metrics" in path:
            injected = self._inject("metric", 429)          # first metric call throttled, recovers
            if injected is not None:
                return injected
            metric = request.url.params.get("metricnames", "")
            key = request.url.params.get("aggregation", "Average").lower()
            series = [98.0] * 7 if metric == "Available Memory Percentage" else [2.0] * 7  # idle-ish
            data = [{key: v} for v in series]
            return httpx.Response(200, json={"value": [{"timeseries": [{"data": data}]}]})

        if "/providers/Microsoft.Advisor/recommendations" in path:
            return httpx.Response(200, json={"value": []})
        if "/providers/Microsoft.Consumption/reservationRecommendations" in path:
            return httpx.Response(200, json={"value": []})

        if "/providers/Microsoft.CostManagement/query" in path:
            injected = self._inject("cost", 503)            # first cost query 5xx, recovers on retry
            if injected is not None:
                return injected
            body = json.loads(request.content.decode())
            grouping = body["dataset"]["grouping"][0]["name"]
            if grouping == "ServiceName":
                return httpx.Response(200, json={"properties": {
                    "columns": [{"name": "Cost"}, {"name": "ServiceName"}, {"name": "Currency"}],
                    "rows": [[VM_COST * N_VMS, "Virtual Machines", "USD"]]}})
            # Per-resource monthly history: two steady months per VM → grounded "last month" basis.
            cols = [{"name": "Cost"}, {"name": "ResourceId"}, {"name": "BillingMonth"}, {"name": "Currency"}]
            rows = []
            for vm in self.vms:
                rows.append([VM_COST, vm["id"], "2025-06-01", "USD"])
                rows.append([VM_COST, vm["id"], "2025-07-01", "USD"])
            return httpx.Response(200, json={"properties": {"columns": cols, "rows": rows}})

        return httpx.Response(200, json={})


@pytest.fixture
def stress_env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(pipeline, "SessionLocal", TestSession)

    async def _instant(_s):
        return None
    monkeypatch.setattr(resilience, "_sleep", _instant)   # don't actually wait out backoff

    def _seed():
        s = TestSession()
        a = Assessment(user_id="u1", user_email="u@x.com", tenant_id="t1",
                       subscription_ids=["sub-1"], status="queued")
        s.add(a); s.commit(); aid = a.id; s.close()
        return aid
    return TestSession, _seed, monkeypatch


async def test_stress_pipeline_partial_but_consistent(stress_env):
    TestSession, seed, monkeypatch = stress_env
    transport = StressTransport()
    monkeypatch.setattr(pipeline, "AzureClient",
                        lambda token, **kw: AzureClient(token, transport=transport, **kw))
    monkeypatch.setattr(pipeline, "get_pricing_engine",
                        lambda currency=None: PricingEngine(transport=transport))
    aid = seed()

    await pipeline.run_assessment(aid, ["sub-1"], "token")

    a = TestSession().get(Assessment, aid)
    findings = TestSession().query(Finding).filter(Finding.assessment_id == aid).all()
    idle = [f for f in findings if f.category == "idle_vms"]
    diag = a.collection_diagnostics

    # (1) concurrency never exceeded the configured per-run cap (and it DID parallelise).
    assert transport.max_in_flight <= settings.azure_max_concurrency
    assert transport.max_in_flight >= 2

    # (2) retries happened and recovered (429 + 5xx injected), counted in diagnostics.
    assert diag["retry"]["throttled_responses"] >= 1
    assert diag["retry"]["server_errors"] >= 1
    assert diag["retry"]["retries"] >= 2

    # (3)(4)(8) pagination completed + every retrieved VM preserved: all 120 analysed → 120 idle findings.
    assert len(idle) == N_VMS

    # (5)(6) the permanently-failed bucket is recorded (not silently "0 snapshots") → run is PARTIAL.
    assert a.status == "completed"
    assert a.data_quality == "partial"
    assert "orphaned_snapshots" in diag["inventory_failed_buckets"]

    # (7) nothing fabricated from failed data: no snapshot finding; every finding is grounded/quantified.
    assert not any(f.category == "orphaned_snapshots" for f in findings)
    assert all(f.evidence_state == "quantified" and f.validation_status == "validated" for f in idle)

    # (9) totals consistent with the Batch-1 evidence model: sum of counted quantified savings.
    expected_annual = round(N_VMS * VM_COST * 12, 2)      # each idle VM grounded at VM_COST/mo, capped
    assert a.total_savings_annual == expected_annual
    assert a.current_monthly_spend == round(VM_COST * N_VMS, 2)


# ── Item 6: multiple simultaneous assessments respect the PROCESS-WIDE budget ────────

class _CountingTransport(httpx.AsyncBaseTransport):
    """Shared across clients; records the GLOBAL max simultaneously in-flight requests."""
    def __init__(self, tracker):
        self.tracker = tracker

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.tracker["cur"] += 1
        self.tracker["max"] = max(self.tracker["max"], self.tracker["cur"])
        try:
            await asyncio.sleep(0.01)
            return httpx.Response(200, json={"value": []})
        finally:
            self.tracker["cur"] -= 1


async def test_simultaneous_assessments_respect_global_cap(monkeypatch):
    # Three "assessments" (three clients, each per-run cap 8) run at once. The PROCESS-WIDE cap must bound
    # their AGGREGATE in-flight requests so N runs can't multiply the per-run cap into uncontrolled traffic.
    async def _instant(_s):
        return None
    monkeypatch.setattr(resilience, "_sleep", _instant)
    monkeypatch.setattr(settings, "azure_global_max_concurrency", 5)   # small, so it clearly binds

    tracker = {"cur": 0, "max": 0}
    transport = _CountingTransport(tracker)
    clients = [AzureClient("t", transport=transport, max_concurrency=8) for _ in range(3)]

    async def _run(client):
        # 15 calls per client × 3 clients = 45 concurrent calls, all sharing the global semaphore.
        await asyncio.gather(*(client.get_subscriptions() for _ in range(15)))

    await asyncio.gather(*(_run(c) for c in clients))
    assert tracker["max"] <= 5      # aggregate never exceeds the process-wide cap
    assert tracker["max"] >= 2      # and they genuinely overlap (not accidentally serial)
