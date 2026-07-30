"""Tests for Cost Management integration + Advisor cross-validation (Step 4)."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from app.services.azure_client import AzureClient
from app.services.cost_management import (
    NEEDS_REVIEW,
    UNVALIDATED,
    VALIDATED,
    area_for_service,
    build_cost_query,
    build_service_cost_query,
    get_actual_cost_by_resource,
    get_actual_cost_by_service,
    parse_cost_rows,
    parse_service_cost_rows,
    spend_by_area,
    validate_savings,
)
from tests.azure_mocks import cost_management_handler

SERVICE_COLUMNS = [{"name": "Cost"}, {"name": "ServiceName"}, {"name": "Currency"}]

RID = "/subscriptions/sub-1/resourceGroups/rg-a/providers/microsoft.compute/virtualmachines/vm-1"


# ── build_cost_query (pure) ─────────────────────────────────────────────────────

def test_build_cost_query_uses_last_complete_month():
    q = build_cost_query(now=datetime(2025, 8, 15, tzinfo=timezone.utc))
    assert q["type"] == "ActualCost"
    assert q["timePeriod"]["from"] == "2025-07-01T00:00:00Z"   # all of last COMPLETE month (July)
    assert q["timePeriod"]["to"] == "2025-07-31T23:59:59Z"
    assert q["dataset"]["grouping"][0]["name"] == "ResourceId"


def test_build_cost_query_month_to_date_fallback():
    q = build_cost_query(now=datetime(2025, 8, 15, tzinfo=timezone.utc), month_to_date=True)
    assert q["timePeriod"]["from"] == "2025-08-01T00:00:00Z"   # current month so far (new-sub fallback)
    assert q["timePeriod"]["to"] == "2025-08-15T23:59:59Z"


# ── parse_cost_rows (pure) ──────────────────────────────────────────────────────

def test_parse_cost_rows_resolves_columns_by_name():
    # Deliberately reorder columns to prove index-by-name resolution.
    payload = {
        "properties": {
            "columns": [
                {"name": "ResourceId"}, {"name": "Cost"}, {"name": "Currency"},
            ],
            "rows": [[RID, 120.0, "USD"]],
        }
    }
    out = parse_cost_rows(payload, days=30)
    assert out[RID.lower()] == 120.0


def test_parse_cost_rows_sums_full_month_without_normalising():
    payload = {
        "properties": {
            "columns": [{"name": "Cost"}, {"name": "ResourceId"}],
            "rows": [[30.0, RID], [30.0, RID]],  # already a full-month figure, no ×30/days
        }
    }
    out = parse_cost_rows(payload)
    assert out[RID.lower()] == 60.0


# ── validate_savings (pure) ─────────────────────────────────────────────────────

def test_validate_within_tolerance_is_validated():
    cost_map = {RID.lower(): 115.0}
    res = validate_savings(120.0, RID, cost_map)  # 120 vs 115 → +4.3%, within 10%
    assert res.status == VALIDATED
    assert res.actual_monthly_cost == 115.0
    assert res.variance_pct == 4.3


def test_validate_estimate_exceeds_actual_needs_review():
    cost_map = {RID.lower(): 50.0}
    res = validate_savings(120.0, RID, cost_map)  # can't save 120 on a $50 resource
    assert res.status == NEEDS_REVIEW
    assert res.variance_pct == 140.0


def test_validate_no_cost_data_is_unvalidated():
    res = validate_savings(120.0, RID, cost_map={})
    assert res.status == UNVALIDATED


def test_validate_no_resource_id_is_unvalidated():
    res = validate_savings(120.0, None, cost_map={RID.lower(): 100.0})
    assert res.status == UNVALIDATED


def test_validate_zero_actual_cost_with_savings_needs_review():
    res = validate_savings(50.0, RID, cost_map={RID.lower(): 0.0})
    assert res.status == NEEDS_REVIEW


# ── get_actual_cost_by_resource (integration via mock) ──────────────────────────

async def test_get_actual_cost_by_resource():
    handler = cost_management_handler(rows=[[120.0, RID, "USD"]])
    client = AzureClient("fake-token", transport=httpx.MockTransport(handler))
    cost_map = await get_actual_cost_by_resource(client, "sub-1", days=30)
    assert cost_map[RID.lower()] == 120.0


async def test_get_actual_cost_handles_no_access():
    handler = cost_management_handler(rows=[], status=403)
    client = AzureClient("fake-token", transport=httpx.MockTransport(handler))
    cost_map = await get_actual_cost_by_resource(client, "sub-1")
    assert cost_map == {}


async def test_cost_falls_back_to_month_to_date_for_new_subscription():
    import json
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        calls.append(body["timePeriod"]["from"])
        # Last complete month is empty (new sub); month-to-date has data.
        rows = [] if len(calls) == 1 else [[120.0, RID, "USD"]]
        return httpx.Response(200, json={"properties": {
            "columns": [{"name": "Cost"}, {"name": "ResourceId"}, {"name": "Currency"}], "rows": rows}})

    client = AzureClient("fake-token", transport=httpx.MockTransport(handler))
    out = await get_actual_cost_by_resource(client, "sub-1")
    assert out[RID.lower()] == 120.0   # recovered via the month-to-date fallback
    assert len(calls) == 2             # tried last complete month first, then MTD


# ── Total spend / per-service / per-area ─────────────────────────────────────────

def test_area_for_service_mapping():
    assert area_for_service("Virtual Machines") == "Compute"
    assert area_for_service("Azure App Service") == "Compute"
    assert area_for_service("Storage") == "Storage"
    assert area_for_service("Azure Backup") == "Storage"
    assert area_for_service("SQL Database") == "Databases"
    assert area_for_service("Azure Cosmos DB") == "Databases"
    assert area_for_service("Bandwidth") == "Network"
    assert area_for_service("VPN Gateway") == "Network"
    assert area_for_service("Some Unknown Meter") == "Other"


def test_build_service_cost_query_groups_by_service():
    q = build_service_cost_query()
    assert q["dataset"]["grouping"][0]["name"] == "ServiceName"
    assert q["type"] == "ActualCost"
    assert q["timeframe"] == "Custom"  # explicit last-complete-month date range


def test_parse_service_cost_rows():
    payload = {
        "properties": {
            "columns": [{"name": "Cost"}, {"name": "ServiceName"}],
            "rows": [[100.0, "Virtual Machines"], [50.0, "Storage"]],
        }
    }
    out = parse_service_cost_rows(payload, days=30)
    assert out["Virtual Machines"] == 100.0
    assert out["Storage"] == 50.0


def test_spend_by_area_aggregates_services():
    services = {
        "Virtual Machines": 100.0, "Azure App Service": 50.0,
        "Storage": 40.0, "SQL Database": 30.0, "Bandwidth": 10.0, "Weird Thing": 5.0,
    }
    area = spend_by_area(services)
    assert area["Compute"] == 150.0
    assert area["Storage"] == 40.0
    assert area["Databases"] == 30.0
    assert area["Network"] == 10.0
    assert area["Other"] == 5.0


async def test_get_actual_cost_by_service():
    handler = cost_management_handler(rows=[[40000.0, "Virtual Machines", "USD"]], columns=SERVICE_COLUMNS)
    client = AzureClient("fake-token", transport=httpx.MockTransport(handler))
    out = await get_actual_cost_by_service(client, "sub-1")
    assert out["Virtual Machines"] == 40000.0


# ── Month-over-month consistency (validation) ────────────────────────────────────

def test_cost_consistency_flags_stable_vs_erratic():
    from app.services.cost_management import cost_consistency
    out = cost_consistency({
        "stable-vm": [100.0, 102.0, 98.0, 101.0],   # ~constant each month → safe to reserve
        "erratic-vm": [0.0, 200.0, 5.0, 190.0],      # big swings → flagged
        "idle-vm": [0.0, 0.0, 0.0, 0.0],             # never billed
    })
    assert out["stable-vm"]["stable"] is True and out["stable-vm"]["billed_months"] == 4
    assert out["erratic-vm"]["stable"] is False
    assert out["idle-vm"]["billed_months"] == 0 and out["idle-vm"]["stable"] is False


def test_parse_monthly_history_groups_by_resource():
    from app.services.cost_management import parse_monthly_history
    payload = {"properties": {
        "columns": [{"name": "Cost"}, {"name": "ResourceId"}, {"name": "BillingMonth"}],
        "rows": [[100.0, RID, "2025-05-01"], [110.0, RID, "2025-06-01"]],
    }}
    assert parse_monthly_history(payload)[RID.lower()] == [100.0, 110.0]


def test_build_monthly_history_query_spans_complete_months():
    from app.services.cost_management import build_monthly_history_query
    q = build_monthly_history_query(months=4, now=datetime(2025, 8, 15, tzinfo=timezone.utc))
    assert q["dataset"]["granularity"] == "Monthly"
    assert q["timePeriod"]["from"] == "2025-04-01T00:00:00Z"   # 4 complete months back
    assert q["timePeriod"]["to"] == "2025-07-31T23:59:59Z"     # end of last complete month (July)


async def test_cost_map_and_consistency_from_one_query():
    from app.services.cost_management import get_cost_map_and_consistency

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"properties": {
            "columns": [{"name": "Cost"}, {"name": "ResourceId"}, {"name": "BillingMonth"}],
            "rows": [[100.0, RID, "2025-05-01"], [110.0, RID, "2025-06-01"]]}})  # two months

    client = AzureClient("fake-token", transport=httpx.MockTransport(handler))
    cost_map, cons = await get_cost_map_and_consistency(client, "sub-1")
    assert cost_map[RID.lower()] == 110.0                 # cost basis = most recent complete month
    assert cons[RID.lower()]["billed_months"] == 2        # steadiness from the same single query
