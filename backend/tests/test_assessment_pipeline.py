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
from app.models.db import Assessment, AssessmentEvent, Finding, InventoryItem
from app.services import assessment as pipeline
from app.services import resilience
from app.services.azure_client import AzureClient
from app.services.pricing import PricingEngine


async def _instant_sleep(_seconds):
    """Patch-in for resilience._sleep so retry backoff doesn't actually wait during tests."""
    return None
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
                       cost_rows=None, service_rows=None, cost_status=200, cost_throttle_first_n=0,
                       memory_available_values=(98.0,) * 7, reservation_recs=None, sql_db_rows=None,
                       vm_rows=None):
    """One handler routing by host+path across all Azure APIs + Retail Prices.

    Defaults: low CPU (avg+max ~2%) and high available memory (~98%, i.e. ~2% used) → the mock
    VM classifies as idle in the happy-path test (both signals must be low, not CPU alone).

    `cost_throttle_first_n` makes the FIRST N Cost Management query calls return HTTP 429 (with a
    Retry-After) and the rest succeed — to exercise throttle-then-recover via the retry layer.
    """
    cm_calls = [0]   # Cost Management query call counter (for cost_throttle_first_n)
    retail = retail_prices_handler()
    advisor = advisor if advisor is not None else []
    reservation_recs = reservation_recs if reservation_recs is not None else []
    sql_db_rows = sql_db_rows if sql_db_rows is not None else []
    vm_rows = vm_rows if vm_rows is not None else [ARG_RUNNING_VM]
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
                rows = list(vm_rows)
            elif "sku.family" in query:   # full SQL DB inventory (reservation reconciliation)
                rows = list(sql_db_rows)
            else:
                rows = []
            return httpx.Response(200, json={"data": rows})
        if "/providers/Microsoft.Consumption/reservationRecommendations" in path:
            return httpx.Response(200, json={"value": reservation_recs})
        if "/providers/Microsoft.Advisor/recommendations" in path:
            return httpx.Response(200, json={"value": advisor})
        if "/providers/Microsoft.CostManagement/query" in path:
            cm_calls[0] += 1
            if cm_calls[0] <= cost_throttle_first_n:
                return httpx.Response(429, headers={"Retry-After": "1"},
                                     json={"error": {"code": "TooManyRequests", "message": "throttled"}})
            if cost_status != 200:
                return httpx.Response(cost_status, json={"error": {"message": "no cost access"}})
            body = json.loads(request.content.decode())
            grouping = body["dataset"]["grouping"][0]["name"]
            if grouping == "ServiceName":
                return httpx.Response(200, json={"properties": {"columns": SERVICE_COLUMNS, "rows": service_rows}})
            # Per-resource cost. The MONTHLY-history query (granularity=Monthly) returns each resource
            # across TWO steady complete months (→ stable, so the representative basis = last month =
            # the row's cost). The month-to-date query (granularity=None) returns the single MTD figure.
            if body["dataset"].get("granularity") == "Monthly":
                month_cols = [{"name": "Cost"}, {"name": "ResourceId"},
                              {"name": "BillingMonth"}, {"name": "Currency"}]
                month_rows = []
                for cost, rid, cur in cost_rows:
                    month_rows.append([cost, rid, "2025-06-01", cur])
                    month_rows.append([cost, rid, "2025-07-01", cur])
                return httpx.Response(200, json={"properties": {"columns": month_cols, "rows": month_rows}})
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
        # Forward **kw (max_concurrency, stats, retries, base_delay) so the pipeline's real client
        # config — concurrency caps and the shared RetryStats — is exercised end-to-end, not dropped.
        monkeypatch.setattr(pipeline, "AzureClient",
                            lambda token, **kw: AzureClient(token, transport=transport, **kw))
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

    # Every saving is GROUNDED in the resource's actual billed cost (Cost Management), never the retail
    # list price: disk 25.0 + IP 4.0 + idle VM 70.08 (payg ≤ its actual 72) = 99.08.
    assert a.total_savings_monthly == 99.08
    assert a.total_savings_annual == round(99.08 * 12, 2)
    assert all(f.evidence_state == "quantified" for f in findings)

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
    # Azure's reservation engine covers NON-VM types (VMs go through the production-targeted VM
    # detector). A real SQL reservation rec surfaces as an authoritative reserved-capacity finding ONLY
    # when the assessed subscription currently has a reservation-eligible (vCore) SQL Database — the
    # reconciliation layer resolves the affected database from CURRENT inventory.
    recs = [{
        "kind": "legacy", "location": "eastus",
        "properties": {
            "resourceType": "SQLDatabases", "normalizedSize": "SQLDB_GP_Gen5", "term": "P1Y",
            "lookBackPeriod": "Last30Days", "scope": "Single", "netSavings": 123.0,
            "costWithNoReservedInstances": 400.0, "totalCostWithReservedInstances": 277.0,
            "recommendedQuantity": 2, "subscriptionId": "sub-1",
        },
    }]
    sql_db = {"id": "/subscriptions/sub-1/resourceGroups/rg/providers/microsoft.sql/servers/srv/databases/db-1",
              "name": "db-1", "subscriptionId": "sub-1", "resourceGroup": "rg", "location": "eastus",
              "tier": "GeneralPurpose", "skuName": "GP_Gen5_2", "family": "Gen5", "capacity": 2}
    install(_composite_handler(reservation_recs=recs, sql_db_rows=[sql_db]))
    aid = seed()

    await pipeline.run_assessment(aid, ["sub-1"], "token")

    s = TestSession()
    ri = s.query(Finding).filter(Finding.category == "sql_db_reserved_capacity").all()
    assert len(ri) == 1
    assert ri[0].estimated_savings_monthly == 123.0
    assert ri[0].details["source"] == "azure_reservation_recommendations"
    assert ri[0].resource_id is None  # SKU-level purchase rec
    # Affected resource resolved from CURRENT inventory — the real DB id, never the subscription id.
    assert ri[0].details["affected_count"] == 1
    assert ri[0].details["affected_vms"][0]["id"].endswith("/databases/db-1")


async def test_pipeline_excludes_advisor_vm_reservation_when_no_current_vm(pipeline_env):
    # LIVE #118 REPRODUCTION through the real pipeline: a subscription with ZERO VMs where Azure ADVISOR
    # returns a VM reservation PURCHASE rec ("Consider virtual machine reserved instance to save over the
    # on-demand costs"). That rec is subscription-scoped and previously leaked into an "Other"/advisor_cost
    # finding using the subscription id as its resource. It must now produce NO finding at all — no
    # advisor_cost, no subscription-id-as-resource, zero VM RI savings.
    TestSession, install, seed = pipeline_env
    sub_id = "005e9433-0d52-4b38-97aa-1b2c3d4e5f60"
    advisor = [{
        "id": f"/subscriptions/{sub_id}/providers/Microsoft.Advisor/recommendations/ADV-RI",
        "properties": {
            "category": "Cost", "impact": "Medium",
            "impactedField": "Microsoft.Subscriptions/subscriptions", "impactedValue": sub_id,
            "shortDescription": {
                "problem": "Consider virtual machine reserved instance to save over the on-demand costs",
                "solution": "Buy a VM reserved instance"},
            "extendedProperties": {"annualSavingsAmount": "396", "savingsAmount": "33"},
            "resourceMetadata": {"resourceId": f"/subscriptions/{sub_id}"},
        },
    }]
    # No VMs and no other inventory → the ONLY thing Azure offers is the Advisor VM reservation rec.
    install(_composite_handler(advisor=advisor, vm_rows=[], cost_rows=[],
                               service_rows=[[100.0, "SQL Database", "USD"]]))
    aid = seed()

    await pipeline.run_assessment(aid, ["sub-1"], "token")

    s = TestSession()
    findings = s.query(Finding).all()
    assert [f for f in findings if f.category == "advisor_cost"] == []      # no "Other" advisor finding
    assert [f for f in findings if f.category == "ri_vm"] == []             # no VM RI finding
    # Nothing may use the subscription id (bare or /subscriptions/{id}) as its affected resource.
    assert all((f.resource_id or "") not in (sub_id, f"/subscriptions/{sub_id}") for f in findings)


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
    # Cost Management denied (no billing access). With NO per-resource billed cost, orphan findings
    # (unattached disk, orphaned IP) cannot be quantified — a retail list price is NOT customer spend —
    # so they surface as REVIEW ("not quantified", reference price only) and contribute ZERO to the
    # savings total. Grounded-only savings (idle/oversized VM, AHB) are SUPPRESSED entirely.
    install(_composite_handler(cost_status=403))
    aid = seed()

    await pipeline.run_assessment(aid, ["sub-1"], "token")

    s = TestSession()
    a = s.get(Assessment, aid)
    assert a.status == "completed"
    findings = s.query(Finding).filter(Finding.assessment_id == aid).all()
    assert a.findings_count == 2                 # disk + IP, both REVIEW
    cats = {f.category for f in findings}
    assert "idle_vms" not in cats and "windows_ahb" not in cats  # grounded-only → suppressed
    assert all(f.evidence_state == "review" for f in findings)   # never a fabricated saving
    assert all(f.estimated_savings_monthly == 0 for f in findings)
    assert a.total_savings_annual == 0           # REVIEW findings never count toward the total
    assert a.cost_data_available == 0
    assert a.current_monthly_spend is None
    assert a.current_annual_spend is None


async def test_cost_management_429_retries_then_recovers(pipeline_env, monkeypatch):
    # (A) Cost Management throttles the first request (HTTP 429) then succeeds. The retry layer waits out
    # the 429 and gets the billing data → spend populated, quantified findings usable, run NOT partial.
    monkeypatch.setattr(resilience, "_sleep", _instant_sleep)   # don't actually wait out backoff
    TestSession, install, seed = pipeline_env
    install(_composite_handler(cost_throttle_first_n=1))
    aid = seed()

    await pipeline.run_assessment(aid, ["sub-1"], "token")

    s = TestSession()
    a = s.get(Assessment, aid)
    assert a.status == "completed"
    assert a.cost_data_available == 1                # billing recovered after the retry
    assert a.current_monthly_spend is not None       # real spend, from the recovered response
    assert a.data_quality == "complete"              # a recovered throttle is NOT a partial run
    assert a.collection_diagnostics["billing_failed_subs"] == 0


async def test_cost_management_429_exhausted_stays_partial_no_fabrication(pipeline_env, monkeypatch):
    # (B) Cost Management stays throttled (every request 429) until retries + the patient second attempt
    # are exhausted. The run must record the failure honestly and never fabricate spend/savings, while
    # resource-based findings (which don't need billing) still surface.
    monkeypatch.setattr(resilience, "_sleep", _instant_sleep)
    monkeypatch.setattr(pipeline, "BILLING_RETRY_DELAY_SECONDS", 0)   # don't wait the ~45s patient retry
    TestSession, install, seed = pipeline_env
    install(_composite_handler(cost_status=429))
    aid = seed()

    await pipeline.run_assessment(aid, ["sub-1"], "token")

    s = TestSession()
    a = s.get(Assessment, aid)
    assert a.status == "completed"                   # the assessment still completes
    assert a.data_quality == "partial"               # honest failure state, not "complete"
    assert a.collection_diagnostics["billing_failed_subs"] >= 1
    assert a.collection_diagnostics["retry"]["throttled_responses"] >= 1
    assert a.cost_data_available == 0
    assert a.current_monthly_spend is None           # NO fabricated current spend
    assert a.current_annual_spend is None            # NO fabricated projected spend
    assert a.total_savings_annual == 0.0             # billing-dependent savings stay unquantified
    # Resource-based findings STILL work without billing (inventory succeeded): disk + IP as REVIEW.
    cats = {f.category for f in s.query(Finding).filter(Finding.assessment_id == aid).all()}
    assert "unattached_managed_disks" in cats and "orphaned_public_ips" in cats


async def test_new_subscription_empty_billing_stays_complete_not_partial(pipeline_env, monkeypatch):
    # (D) A genuinely new subscription: Cost Management returns HTTP 200 with NO rows (no history yet).
    # Billing collection SUCCEEDED (empty), so this is COMPLETE — clearly distinct from the 429 failure
    # above (which is PARTIAL). No fabricated spend either.
    monkeypatch.setattr(resilience, "_sleep", _instant_sleep)
    TestSession, install, seed = pipeline_env
    install(_composite_handler(cost_rows=[], service_rows=[]))
    aid = seed()

    await pipeline.run_assessment(aid, ["sub-1"], "token")

    s = TestSession()
    a = s.get(Assessment, aid)
    assert a.status == "completed"
    assert a.cost_data_available == 0
    assert a.data_quality == "complete"              # empty-but-successful billing != a throttle failure
    assert a.collection_diagnostics["billing_failed_subs"] == 0
    assert a.current_monthly_spend is None           # no fabricated spend


async def test_cost_management_exhausts_then_recovers_clears_stale_partial(pipeline_env, monkeypatch):
    # (#127 REGRESSION) The first cost-map query exhausts all its 429 retries (billing marked failed AND a
    # retry chain exhausted), but the outer cost-map re-fetch then succeeds. Because billing ULTIMATELY
    # recovered, the run must be COMPLETE: the stale billing-failed flag is cleared and the recovered
    # (transient) exhaustion is forgiven, with spend populated and normal grounded findings.
    from app.services.azure_client import COST_MANAGEMENT_MAX_RETRIES
    monkeypatch.setattr(resilience, "_sleep", _instant_sleep)          # skip retry backoff
    monkeypatch.setattr(pipeline, "COST_MAP_RETRY_DELAY_SECONDS", 0)   # skip the 25s cost-map re-fetch wait
    TestSession, install, seed = pipeline_env
    # Throttle exactly the first cost-map query to exhaustion (initial + every retry), then let the
    # service query and the outer cost-map re-fetch succeed.
    install(_composite_handler(cost_throttle_first_n=COST_MANAGEMENT_MAX_RETRIES + 1))
    aid = seed()

    await pipeline.run_assessment(aid, ["sub-1"], "token")

    s = TestSession()
    a = s.get(Assessment, aid)
    diag = a.collection_diagnostics
    assert diag["retry"]["throttled_responses"] >= 1   # throttling genuinely occurred (telemetry preserved)
    assert a.cost_data_available == 1                  # billing recovered
    assert a.current_monthly_spend is not None         # real spend populated
    assert diag["billing_failed_subs"] == 0            # stale failure flag CLEARED (the fix)
    assert diag["retry"]["exhausted"] == 0             # the recovered billing exhaustion is forgiven
    assert a.data_quality == "complete"                # no longer falsely partial
    findings = s.query(Finding).filter(Finding.assessment_id == aid).all()
    assert len(findings) >= 1 and all(f.evidence_state == "quantified" for f in findings)  # normal grounding


async def test_cost_management_200_immediately_is_complete(pipeline_env, monkeypatch):
    # (TEST 3) No throttling at all: existing behavior unchanged — billing available, nothing failed.
    monkeypatch.setattr(resilience, "_sleep", _instant_sleep)
    TestSession, install, seed = pipeline_env
    install(_composite_handler())                      # 200 on the first request
    aid = seed()

    await pipeline.run_assessment(aid, ["sub-1"], "token")

    a = TestSession().get(Assessment, aid)
    assert a.cost_data_available == 1
    assert a.collection_diagnostics["billing_failed_subs"] == 0
    assert a.collection_diagnostics["retry"]["exhausted"] == 0
    assert a.data_quality == "complete"


async def test_billing_recovers_but_other_collector_failure_is_partial(pipeline_env, monkeypatch):
    # (TEST 4) Billing succeeds, but an INDEPENDENT collector (metrics) genuinely fails. The run is PARTIAL
    # only because of the metrics failure; billing is NOT marked failed, and spend is still available.
    monkeypatch.setattr(resilience, "_sleep", _instant_sleep)

    async def _metrics_fail(client, vms, days=30, report=None):
        if report is not None:
            report.note_metrics(len(vms), len(vms))    # every VM's metric call failed
        return [{**vm, "max_cpu": None, "avg_cpu": None, "cpu_datapoints": 0,
                 "peak_memory_used_pct": None, "memory_available": False, "metrics_failed": True} for vm in vms]
    monkeypatch.setattr(pipeline, "enrich_vms_with_metrics", _metrics_fail)
    TestSession, install, seed = pipeline_env
    install(_composite_handler())                      # billing 200
    aid = seed()

    await pipeline.run_assessment(aid, ["sub-1"], "token")

    a = TestSession().get(Assessment, aid)
    assert a.cost_data_available == 1                              # billing succeeded
    assert a.collection_diagnostics["billing_failed_subs"] == 0    # billing NOT marked failed
    assert a.collection_diagnostics["metrics_failed"] >= 1
    assert a.data_quality == "partial"                            # partial ONLY due to metrics


async def test_pipeline_degraded_billing_marks_findings_review(pipeline_env, monkeypatch):
    # The SUBSCRIPTION total came through but the PER-RESOURCE cost query returned nothing (Cost
    # Management throttled the heavier query). With no billed cost to ground them, orphan findings
    # (unattached disk, orphaned IP) are REVIEW ("not quantified", reference price only) — never a
    # fabricated saving — the run is flagged billing_detail_unavailable, and the total stays zero.
    TestSession, install, seed = pipeline_env
    monkeypatch.setattr(pipeline, "COST_MAP_RETRY_DELAY_SECONDS", 0)  # don't wait out the (mock) throttle
    install(_composite_handler(cost_rows=[]))   # empty per-resource cost; service-level spend present
    aid = seed()

    await pipeline.run_assessment(aid, ["sub-1"], "token")

    s = TestSession()
    a = s.get(Assessment, aid)
    assert a.status == "completed"
    assert a.billing_detail_unavailable == 1
    findings = s.query(Finding).filter(Finding.assessment_id == aid).all()
    assert findings and all(f.evidence_state == "review" for f in findings)  # nothing fabricated
    assert a.total_savings_annual == 0                                       # no quantified savings
    assert a.current_monthly_spend is not None and a.current_monthly_spend > 0  # sub total still shown


# ── Data-collection completeness (Batch 2): missing data is NOT zero / never a false "complete" ─────

async def test_inventory_bucket_failure_marks_run_partial(pipeline_env, monkeypatch):
    # One Resource Graph bucket fails (throttle/error after retries). It must NOT read as "no resources
    # of that type": the run is flagged PARTIAL with a concise client message, still completing.
    TestSession, install, seed = pipeline_env
    install(_composite_handler())
    aid = seed()

    real = pipeline.collect_inventory

    async def _one_bucket_fails(client, subs):
        inventory, errors = await real(client, subs)
        errors["orphaned_public_ips"] = "throttled after retries"   # simulate a failed bucket
        inventory["orphaned_public_ips"] = []
        return inventory, errors
    monkeypatch.setattr(pipeline, "collect_inventory", _one_bucket_fails)

    await pipeline.run_assessment(aid, ["sub-1"], "token")

    a = TestSession().get(Assessment, aid)
    assert a.status == "completed"
    assert a.data_quality == "partial"
    assert a.data_quality_message and "could not be collected" in a.data_quality_message
    assert a.collection_diagnostics["inventory_failed_buckets"] == ["orphaned_public_ips"]


async def test_inventory_summary_failure_leaves_count_unknown_not_zero(pipeline_env, monkeypatch):
    # The resource-COUNT query fails. The count must be recorded as UNKNOWN (None), not a fabricated 0,
    # and the run flagged PARTIAL.
    TestSession, install, seed = pipeline_env
    install(_composite_handler())
    aid = seed()

    async def _summary_fails(client, subs):
        return 0, 0, [], False   # ok=False → count unknown
    monkeypatch.setattr(pipeline, "_gather_inventory_summary", _summary_fails)

    await pipeline.run_assessment(aid, ["sub-1"], "token")

    a = TestSession().get(Assessment, aid)
    assert a.data_quality == "partial"
    assert a.collection_diagnostics["resources_discovered"] is None       # UNKNOWN, never a false 0
    assert a.collection_diagnostics["inventory_summary_failed"] is True


async def test_metrics_failure_marks_partial_not_idle(pipeline_env, monkeypatch):
    # Metrics collection fails for the VMs. A failed metric must NOT be read as 0% utilisation / idle —
    # the VM is left un-classifiable (no idle finding) and the run is flagged PARTIAL.
    TestSession, install, seed = pipeline_env
    install(_composite_handler())
    aid = seed()

    async def _metrics_fail(client, vms, days=30, report=None):
        if report is not None:
            report.note_metrics(len(vms), len(vms))    # every VM's metric call failed
        return [{**vm, "max_cpu": None, "avg_cpu": None, "cpu_datapoints": 0,
                 "peak_memory_used_pct": None, "memory_available": False,
                 "metric_window_days": days, "metrics_failed": True} for vm in vms]
    monkeypatch.setattr(pipeline, "enrich_vms_with_metrics", _metrics_fail)

    await pipeline.run_assessment(aid, ["sub-1"], "token")

    a = TestSession().get(Assessment, aid)
    cats = {f.category for f in TestSession().query(Finding).filter(Finding.assessment_id == aid)}
    assert "idle_vms" not in cats                       # failed metrics never become "idle"
    assert a.data_quality == "partial"
    assert a.collection_diagnostics["metrics_failed"] >= 1


async def test_billing_transient_failure_marks_partial(pipeline_env, monkeypatch):
    # A transient 500 on Cost Management (after retries) is a FAILURE, not "zero spend" → PARTIAL.
    TestSession, install, seed = pipeline_env
    monkeypatch.setattr(resilience, "_sleep", _instant_sleep)   # don't actually wait out the backoff
    monkeypatch.setattr(pipeline, "BILLING_RETRY_DELAY_SECONDS", 0)   # nor the patient billing re-attempt
    install(_composite_handler(cost_status=500))
    aid = seed()

    await pipeline.run_assessment(aid, ["sub-1"], "token")

    a = TestSession().get(Assessment, aid)
    assert a.status == "completed"
    assert a.data_quality == "partial"
    assert a.collection_diagnostics["billing_failed_subs"] >= 1


async def test_all_inventory_failed_marks_failed_no_false_complete(pipeline_env, monkeypatch):
    # Every inventory bucket fails → no defensible view of the environment → FAILED (never a clean
    # "complete, no findings" assessment that would read as "well-optimised").
    from app.services.kql import filtered_inventory_queries
    TestSession, install, seed = pipeline_env
    install(_composite_handler())
    aid = seed()

    async def _all_fail(client, subs):
        buckets = list(filtered_inventory_queries().keys())
        return {b: [] for b in buckets}, {b: "throttled after retries" for b in buckets}
    monkeypatch.setattr(pipeline, "collect_inventory", _all_fail)

    await pipeline.run_assessment(aid, ["sub-1"], "token")

    a = TestSession().get(Assessment, aid)
    assert a.data_quality == "failed"
    assert a.findings_count == 0
    assert a.total_savings_annual == 0
    assert a.data_quality_message and "could not be collected" in a.data_quality_message


async def test_pipeline_derives_spend_from_per_resource_when_service_query_empty(pipeline_env):
    TestSession, install, seed = pipeline_env
    # Cost Management returned PER-RESOURCE cost (findings are grounded) but the SERVICE-level spend query
    # came back empty (a common transient throttle). The current spend must be DERIVED from the
    # per-resource cost — not left blank as "Awaiting billing data" — and projected spend must show.
    install(_composite_handler(service_rows=[]))
    aid = seed()

    await pipeline.run_assessment(aid, ["sub-1"], "token")

    s = TestSession()
    a = s.get(Assessment, aid)
    assert a.status == "completed"
    assert a.cost_data_available == 1                    # we DO have cost (per-resource), so not "awaiting"
    assert a.current_monthly_spend is not None and a.current_monthly_spend > 0
    assert a.spend_estimated == 1                        # derived from per-resource cost → flagged estimate
    # Derived spend = sum of the per-resource cost basis (disk 25 + VM 72 + IP 4 = 101).
    assert a.current_monthly_spend == 101.0
    assert a.current_annual_spend == round(101.0 * 12, 2)


async def test_pipeline_caps_overestimate_to_actual_cost(pipeline_env):
    TestSession, install, seed = pipeline_env
    # Disk actually costs only $2 → its $19.71 estimate is capped to $2 and reads as validated.
    install(_composite_handler(cost_rows=[[2.0, DISK_ID, "USD"], [72.0, VM_ID, "USD"], [4.0, IP_ID, "USD"]]))
    aid = seed()

    await pipeline.run_assessment(aid, ["sub-1"], "token")

    s = TestSession()
    a = s.get(Assessment, aid)
    disk = s.query(Finding).filter(Finding.category == "unattached_managed_disks").one()
    assert disk.estimated_savings_monthly == 2.0       # capped to actual
    assert disk.validation_status == "validated"       # not a false-alarm "needs review"
    assert a.needs_review_count == 0


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


async def test_events_report_real_counts_in_order(pipeline_env):
    # The live event stream is rendered verbatim to the client, so every event must correspond to
    # work the pipeline actually did and carry the run's real numbers — never a placeholder.
    TestSession, install, seed = pipeline_env
    install(_composite_handler())
    aid = seed()

    await pipeline.run_assessment(aid, ["sub-1"], "token")

    s = TestSession()
    events = (s.query(AssessmentEvent)
              .filter(AssessmentEvent.assessment_id == aid)
              .order_by(AssessmentEvent.id).all())
    messages = [e.message for e in events]

    assert messages[0] == "Connected to Azure"
    assert "Assessing 1 subscription" in messages[1]   # singular, from the real sub count
    assert messages[-1] == "Assessment complete"

    # The findings count in the event must match what was actually persisted.
    persisted = s.query(Finding).filter(Finding.assessment_id == aid).count()
    identified = [m for m in messages if m.startswith("Identified ")]
    assert identified and str(persisted) in identified[0]

    # Every event is stamped with the stage it happened in, and ids increase monotonically.
    assert all(e.stage for e in events)
    assert [e.id for e in events] == sorted(e.id for e in events)


async def test_scan_counts_are_published_before_the_run_finishes(pipeline_env, monkeypatch):
    # The live progress UI shows REAL discovered counts while the assessment is still running, so
    # the inventory summary must be persisted at the inventory phase — not held back to the end.
    TestSession, install, seed = pipeline_env
    install(_composite_handler())
    aid = seed()

    seen = {}
    original = pipeline.enrich_vms_with_metrics

    async def _spy(client, vms, **kwargs):
        # Step 2 (metrics) runs after inventory — the counts must already be readable by now.
        s = TestSession()
        a = s.get(Assessment, aid)
        seen["total"] = a.total_resources
        seen["types"] = a.resource_type_count
        seen["status"] = a.status
        s.close()
        return await original(client, vms, **kwargs)

    monkeypatch.setattr(pipeline, "enrich_vms_with_metrics", _spy)

    await pipeline.run_assessment(aid, ["sub-1"], "token")

    assert seen["status"] != "completed"      # genuinely mid-run, not an end-of-run read
    assert seen["total"] and seen["total"] > 0
    assert seen["types"] and seen["types"] > 0


def test_only_realisable_savings_counted_in_headline_total(pipeline_env):
    # Conditional Azure Hybrid Benefit savings (windows_ahb / sql_ahb) are realised ONLY if the customer
    # already owns eligible licences, so they must NOT inflate the headline total — they're surfaced
    # separately as "potential". Only realisable findings (e.g. the RI) count toward the total; every
    # finding still contributes to findings_count.
    TestSession, _install, seed = pipeline_env
    aid = seed()
    findings = [
        {"category": "ri_vm", "display_name": "RI", "resource_type": "vm",
         "estimated_savings_monthly": 100.0, "estimated_savings_annual": 1200.0},
        {"category": "windows_ahb", "display_name": "AHB", "resource_type": "vm",
         "estimated_savings_monthly": 900.0, "estimated_savings_annual": 10800.0},
        {"category": "sql_ahb", "display_name": "SQL AHB", "resource_type": "sql",
         "estimated_savings_monthly": 50.0, "estimated_savings_annual": 600.0},
    ]
    s = TestSession()
    pipeline._persist_findings_and_totals(s, aid, findings)
    s.close()

    a = TestSession().get(Assessment, aid)
    assert a.total_savings_monthly == 100.0     # RI only; AHB (900 + 50) excluded from the headline
    assert a.total_savings_annual == 1200.0     # RI only; AHB (10800 + 600) excluded
    assert a.findings_count == 3                # all three are still recorded as findings
