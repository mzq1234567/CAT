"""
End-to-end integration test for the assessment pipeline (Step 11).

Mocks every Azure dependency (Resource Graph, Advisor, Cost Management, Monitor metrics, Retail
Prices) behind one MockTransport and drives `run_assessment` against an in-memory DB, asserting the
state machine reaches COMPLETED and the findings + totals are correct.
"""
from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.db import Assessment, Finding, InventoryItem
from app.services import assessment as pipeline
from app.services.azure_client import AzureClient
from app.services.pricing import PricingEngine
from tests.azure_mocks import (
    ARG_ORPHANED_IP,
    ARG_RUNNING_VM,
    ARG_UNATTACHED_DISK,
    COST_COLUMNS,
    retail_prices_handler,
)

DISK_ID = ARG_UNATTACHED_DISK["id"]
IP_ID = ARG_ORPHANED_IP["id"]
VM_ID = ARG_RUNNING_VM["id"]


SERVICE_COLUMNS = [{"name": "Cost"}, {"name": "ServiceName"}, {"name": "Currency"}]


def _composite_handler(*, metric_values=(2.0,) * 7, max_metric_values=None, advisor=None,
                       cost_rows=None, service_rows=None, cost_status=200,
                       memory_available_values=(98.0,) * 7, reservation_recs=None):
    """One handler routing by host+path across all Azure APIs + Retail Prices.

    Defaults: low CPU (avg+max ~2%) and high available memory (~98%, i.e. ~2% used) → the mock
    VM classifies as idle in the happy-path test (both signals must be low, not CPU alone).
    """
    retail = retail_prices_handler()
    advisor = advisor if advisor is not None else []
    reservation_recs = reservation_recs if reservation_recs is not None else []
    cost_rows = cost_rows if cost_rows is not None else [
        [25.0, DISK_ID, "USD"], [72.0, VM_ID, "USD"], [4.0, IP_ID, "USD"],
    ]
    service_rows = service_rows if service_rows is not None else [
        [40000.0, "Virtual Machines", "USD"], [8000.0, "Storage", "USD"],
    ]
    # Default peak CPU low too → VM classifies as idle in the happy-path test.
    max_metric_values = max_metric_values if max_metric_values is not None else metric_values

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path
        if host == "prices.azure.com":
            return retail(request)
        # management.azure.com
        if path.endswith("/subscriptions"):
            return httpx.Response(200, json={"value": [{"subscriptionId": "sub-1", "state": "Enabled"}]})
        if "/providers/Microsoft.ResourceGraph/resources" in path:
            query = json.loads(request.content.decode())["query"]
            if "summarize" in query:  # full-inventory count-by-type
                return httpx.Response(200, json={"data": [
                    {"type": "microsoft.compute/virtualmachines", "resourceCount": 30},
                    {"type": "microsoft.storage/storageaccounts", "resourceCount": 12},
                    {"type": "microsoft.network/networkinterfaces", "resourceCount": 8},
                ]})
            if "== 'Unattached'" in query:
                rows = [ARG_UNATTACHED_DISK]
            elif "isnull(properties.ipConfiguration)" in query:
                rows = [ARG_ORPHANED_IP]
            elif "Windows_Server" in query:  # windows-without-AHB query (also contains 'VM running')
                rows = []
            elif "'VM running'" in query:
                rows = [ARG_RUNNING_VM]
            else:
                rows = []
            return httpx.Response(200, json={"data": rows})
        if "/providers/Microsoft.Consumption/reservationRecommendations" in path:
            return httpx.Response(200, json={"value": reservation_recs})
        if "/providers/Microsoft.Advisor/recommendations" in path:
            return httpx.Response(200, json={"value": advisor})
        if "/providers/Microsoft.CostManagement/query" in path:
            if cost_status != 200:
                return httpx.Response(cost_status, json={"error": {"message": "no cost access"}})
            grouping = json.loads(request.content.decode())["dataset"]["grouping"][0]["name"]
            if grouping == "ServiceName":
                return httpx.Response(200, json={"properties": {"columns": SERVICE_COLUMNS, "rows": service_rows}})
            return httpx.Response(200, json={"properties": {"columns": COST_COLUMNS, "rows": cost_rows}})
        if "/providers/microsoft.insights/metrics" in path:
            metric_name = request.url.params.get("metricnames", "")
            aggregation = request.url.params.get("aggregation", "Average")
            key = aggregation.lower()
            if metric_name == "Available Memory Percentage":
                series = memory_available_values
            else:
                series = max_metric_values if aggregation == "Maximum" else metric_values
            data = [{key: v} for v in series]
            return httpx.Response(200, json={"value": [{"timeseries": [{"data": data}]}]})
        return httpx.Response(200, json={})

    return handler


@pytest.fixture
def pipeline_env(monkeypatch):
    """Wire an in-memory DB + mocked Azure/pricing into the assessment module."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    monkeypatch.setattr(pipeline, "SessionLocal", TestSession)

    def _install(handler):
        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(pipeline, "AzureClient",
                            lambda token, **kw: AzureClient(token, transport=transport))
        monkeypatch.setattr(pipeline, "get_pricing_engine",
                            lambda currency=None: PricingEngine(transport=transport))

    def _seed():
        s = TestSession()
        a = Assessment(user_id="u1", user_email="u@x.com", tenant_id="t1",
                       subscription_ids=["sub-1"], status="queued")
        s.add(a)
        s.commit()
        aid = a.id
        s.close()
        return aid

    return TestSession, _install, _seed


async def test_full_pipeline_produces_findings_and_totals(pipeline_env):
    TestSession, install, seed = pipeline_env
    install(_composite_handler())  # low CPU → VM classified idle
    aid = seed()

    await pipeline.run_assessment(aid, ["sub-1"], "token")

    s = TestSession()
    a = s.get(Assessment, aid)
    findings = s.query(Finding).filter(Finding.assessment_id == aid).all()
    inventory = s.query(InventoryItem).filter(InventoryItem.assessment_id == aid).all()

    # State machine completed with the snapshot stamped.
    assert a.status == "completed"
    assert a.progress == 100
    assert a.snapshot_at is not None

    # Three findings: unattached disk, orphaned IP, idle VM.
    categories = {f.category for f in findings}
    assert categories == {"unattached_managed_disks", "orphaned_public_ips", "idle_vms"}
    assert a.findings_count == 3

    # Totals: disk 19.71 + IP 3.65 + idle VM 70.08 = 93.44 (live retail prices).
    assert a.total_savings_monthly == 93.44
    assert a.total_savings_annual == round(93.44 * 12, 2)

    # All three validated against actual cost (estimate within tolerance).
    assert a.needs_review_count == 0
    assert all(f.confidence > 0 for f in findings)
    assert all(f.validation_status == "validated" for f in findings)

    # Actual spend surfaced from Cost Management (VM 40k + Storage 8k / month).
    assert a.cost_data_available == 1
    assert a.current_monthly_spend == 48000.0
    assert a.current_annual_spend == 576000.0
    assert a.spend_by_area == {"Compute": 40000.0, "Storage": 8000.0}

    # Inventory persisted (disk + ip + running vm buckets each have a row).
    assert len(inventory) == 3

    # Full-inventory summary captured (30 + 12 + 8 resources across 3 types).
    assert a.total_resources == 50
    assert a.resource_type_count == 3


def test_dedupe_keeps_one_finding_per_resource():
    rid = "/subscriptions/s/resourceGroups/rg/providers/microsoft.compute/virtualmachines/vm-1"
    findings = [
        {"resource_id": rid, "category": "idle_vms", "estimated_savings_annual": 6728.0},
        {"resource_id": rid.upper(), "category": "advisor_cost", "estimated_savings_annual": 3000.0},
        {"resource_id": "/subscriptions/s/.../disk-1", "category": "unattached_managed_disks", "estimated_savings_annual": 200.0},
        {"resource_id": None, "category": "sub_level", "estimated_savings_annual": 50.0},
    ]
    out = pipeline._dedupe(findings)
    # vm-1 (case-insensitive) collapses to its highest-savings finding; the rest pass through.
    assert len(out) == 3
    vm = [f for f in out if (f.get("resource_id") or "").lower() == rid.lower()]
    assert len(vm) == 1 and vm[0]["estimated_savings_annual"] == 6728.0


async def test_pipeline_uses_reservation_recommendations(pipeline_env):
    TestSession, install, seed = pipeline_env
    # Azure returns a real reservation recommendation → it must surface as an authoritative RI finding.
    recs = [{
        "kind": "legacy", "location": "eastus",
        "properties": {
            "resourceType": "virtualmachines", "normalizedSize": "Standard_D2s_v3", "term": "P1Y",
            "lookBackPeriod": "Last30Days", "scope": "Single", "netSavings": 123.0,
            "costWithNoReservedInstances": 400.0, "totalCostWithReservedInstances": 277.0,
            "recommendedQuantity": 2, "subscriptionId": "sub-1",
        },
    }]
    install(_composite_handler(reservation_recs=recs))
    aid = seed()

    await pipeline.run_assessment(aid, ["sub-1"], "token")

    s = TestSession()
    ri = s.query(Finding).filter(Finding.category == "ri_vm").all()
    assert len(ri) == 1
    assert ri[0].estimated_savings_monthly == 123.0
    assert ri[0].details["source"] == "azure_reservation_recommendations"
    assert ri[0].resource_id is None  # SKU-level purchase rec


async def test_pipeline_detects_billing_currency(pipeline_env):
    TestSession, install, seed = pipeline_env
    # Cost Management returns costs in CAD → the assessment records CAD (drives currency display).
    install(_composite_handler(service_rows=[[40000.0, "Virtual Machines", "CAD"],
                                             [8000.0, "Storage", "CAD"]]))
    aid = seed()
    await pipeline.run_assessment(aid, ["sub-1"], "token")
    s = TestSession()
    assert s.get(Assessment, aid).currency == "CAD"


async def test_pipeline_without_cost_access(pipeline_env):
    TestSession, install, seed = pipeline_env
    # Cost Management denied (no billing access) — findings must still be produced.
    install(_composite_handler(cost_status=403))
    aid = seed()

    await pipeline.run_assessment(aid, ["sub-1"], "token")

    s = TestSession()
    a = s.get(Assessment, aid)
    assert a.status == "completed"
    assert a.findings_count == 3                 # savings still found without cost access
    assert a.total_savings_annual > 0
    assert a.cost_data_available == 0            # but no spend data
    assert a.current_monthly_spend is None
    assert a.current_annual_spend is None


async def test_pipeline_flags_needs_review(pipeline_env):
    TestSession, install, seed = pipeline_env
    # Disk actually costs only $2 → its $19.71 estimate exceeds actual → needs_review.
    install(_composite_handler(cost_rows=[[2.0, DISK_ID, "USD"], [72.0, VM_ID, "USD"], [4.0, IP_ID, "USD"]]))
    aid = seed()

    await pipeline.run_assessment(aid, ["sub-1"], "token")

    s = TestSession()
    a = s.get(Assessment, aid)
    disk = s.query(Finding).filter(Finding.category == "unattached_managed_disks").one()
    assert disk.validation_status == "needs_review"
    assert a.needs_review_count == 1


async def test_pipeline_marks_failed_on_error(pipeline_env, monkeypatch):
    TestSession, install, seed = pipeline_env
    install(_composite_handler())
    aid = seed()

    async def _boom(*args, **kwargs):
        raise RuntimeError("resource graph exploded")

    monkeypatch.setattr(pipeline, "collect_inventory", _boom)

    await pipeline.run_assessment(aid, ["sub-1"], "token")

    s = TestSession()
    a = s.get(Assessment, aid)
    assert a.status == "failed"
    assert "resource graph exploded" in a.error_message
