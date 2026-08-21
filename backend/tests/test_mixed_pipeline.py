"""
Comprehensive MULTI-CATEGORY integration test for the finding-generation pipeline.

This exercises EVERY currently-active detector simultaneously against a single hand-built mock
environment, then drives the real `assessment._detect_all` pipeline end to end:

    inventory + reservation recs + advisor recs
      -> every detector runs
      -> VM RI reconciliation  (reconcile_vm_recommendations)
      -> SQL RI reconciliation (reconcile_sql_recommendations)
      -> aggregation (commitments_from_recommendations, AHB, deallocated)
      -> zero-saving noise filter
      -> de-dup (_dedupe)
      -> overlap resolution (resolve_overlaps + flag_reservation_rightsizing_overlaps)
      -> final findings

It asserts, for the whole pipeline: the exact set of categories produced, each finding's savings,
its affected resource ids, evidence/validation state, no duplicate findings, no finding suppressed by
another (counted == estimated), and that the total equals the sum of the individual findings. It also
includes NEGATIVE CONTROLS (resources that must NOT become findings) and verifies stale/other-sub/
subscription-scoped inputs are excluded.

TEST ONLY — no production code is imported-for-mutation or changed. Reuses the existing mock pricing
(`FakePricing`) and the existing reservation-group helpers from `tests.test_findings`, using the SAME
eligibility inputs each detector's own unit tests use (no weakened conditions).
"""
from __future__ import annotations

from app.services import assessment as pipeline
from app.services.findings import FindingsEngine, build_advisor_index
from tests.test_findings import (
    DEFAULT_VM_PRICES,
    FakePricing,
    _sql_ri_group,
    _vm,
    _vm_ri_group,
)

# Windows-vs-compute retail (same premium the AHB unit tests use) so the AHB licence fraction is real.
_AHB_WIN = {"Standard_D2s_v3": 137.24, "Standard_D16s_v3": 1097.92}
_AHB_FRAC_D16 = (1097.92 - 560.64) / 1097.92          # licence share of the Windows retail price
AHB_EXPECTED = round(300.0 * _AHB_FRAC_D16, 2)         # actual billed 300 × licence fraction


class MixedPricing(FakePricing):
    """FakePricing with the AHB Windows premium AND a Premium≠Standard disk price (so disk right-sizing
    has a real delta). Everything else is the default fake ladder. Purely a test double."""

    def __init__(self):
        super().__init__(vm_prices=dict(DEFAULT_VM_PRICES), win=dict(_AHB_WIN))

    async def get_managed_disk_monthly_price(self, region, sku, size):
        return 40.0 if "premium" in (sku or "").lower() else 12.0


def _rid(rtype: str, name: str, sub: str = "sub-1", rg: str = "rg-a") -> str:
    return f"/subscriptions/{sub}/resourcegroups/{rg}/providers/{rtype}/{name}"


VMT = "microsoft.compute/virtualmachines"
DBT = "microsoft.sql/servers/srv/databases"
MIT = "microsoft.sql/managedinstances"


async def _run_mixed_environment():
    """Build the mock environment, run the real _detect_all pipeline, return (findings, ids, cost_map)."""
    cost_map: dict = {}

    def bill(rid: str, amount: float) -> str:
        cost_map[rid.lower()] = amount
        return rid

    # ── resource ids (distinct per category; §4 resource isolation) ──────────────────────────────
    ids = {
        "ahb": _rid(VMT, "vm-ahb-01"),
        "dealloc": _rid(VMT, "vm-deallocated-01"),
        "dealloc_os": _rid("microsoft.compute/disks", "disk-dealloc-os"),
        "dealloc_data": _rid("microsoft.compute/disks", "disk-dealloc-data"),
        "dealloc_nocost": _rid(VMT, "vm-deallocated-nocost"),   # negative: no billable disk cost
        "rightsize": _rid(VMT, "vm-rightsize-01"),
        "idle": _rid(VMT, "vm-idle-01"),
        "healthy": _rid(VMT, "vm-healthy-01"),                  # negative: well-utilised
        "ri": _rid(VMT, "vm-ri-01"),                            # current VM the RI resolves to
        "sql_ri": _rid(DBT, "sql-ri-01"),
        "sql_dtu": _rid(DBT, "sql-dtu-01"),                     # negative: DTU, not RI-eligible
        "disk_orphan": _rid("microsoft.compute/disks", "disk-orphan-01"),
        "pip": _rid("microsoft.network/publicipaddresses", "pip-orphan-01"),
        "snap": _rid("microsoft.compute/snapshots", "snap-orphan-01"),
        "nat": _rid("microsoft.network/natgateways", "nat-orphan-01"),
        "bastion": _rid("microsoft.network/bastionhosts", "bastion-01"),
        "lb": _rid("microsoft.network/loadbalancers", "lb-review-01"),   # REVIEW (no billed cost)
        "idle_asp": _rid("microsoft.web/serverfarms", "asp-idle-01"),
        "asp_rs": _rid("microsoft.web/serverfarms", "asp-rightsize-01"),
        "sqldb_rs": _rid(DBT, "db-rightsize-01"),
        "mi_rs": _rid(MIT, "mi-rightsize-01"),
        "disk_rs": _rid("microsoft.compute/disks", "disk-rightsize-01"),
        "paused": _rid(DBT, "db-paused-01"),
        "stopped_mi": _rid(MIT, "mi-stopped-01"),
        "advisor_res": _rid("microsoft.storage/storageaccounts", "stor-advisor-01"),
    }

    # ── VMs evaluated for utilisation (idle / oversized / well-utilised) ─────────────────────────
    vm_oversized = _vm(max_cpu=15.0, peak_memory=20.0, sku="Standard_D16s_v3", rid=ids["rightsize"])
    vm_oversized["name"] = "vm-rightsize-01"
    bill(ids["rightsize"], 600.0)
    vm_idle = _vm(max_cpu=3.0, peak_memory=6.0, sku="Standard_D2s_v3", rid=ids["idle"])
    vm_idle["name"] = "vm-idle-01"
    bill(ids["idle"], 100.0)                                    # idle saving = list price 70.08, ≤ actual
    vm_healthy = _vm(max_cpu=44.0, peak_memory=30.0, avg_cpu=1.2, sku="Standard_D16s_v3", rid=ids["healthy"])
    vm_healthy["name"] = "vm-healthy-01"
    bill(ids["healthy"], 600.0)                                 # negative: no candidate fits
    enriched_running = [vm_oversized, vm_idle, vm_healthy]

    # The RI VM exists in CURRENT inventory (so the historical RI reconciles to a real resource) but its
    # metrics aren't collected → no utilisation finding for it. Its SKU is UNIQUE (F4s_v2) so it can't
    # overlap the D-series right-sizing/idle findings.
    vm_ri_raw = {"id": ids["ri"], "name": "vm-ri-01", "subscriptionId": "sub-1", "location": "eastus",
                 "vmSize": "Standard_F4s_v2", "powerState": "VM running"}
    inventory_running = enriched_running + [vm_ri_raw]

    # ── deallocated VMs (one billable, one negative-control with no cost) ─────────────────────────
    dealloc_vm = {"id": ids["dealloc"], "name": "vm-deallocated-01", "subscriptionId": "sub-1",
                  "location": "eastus", "vmSize": "Standard_D2s_v3", "powerState": "VM deallocated",
                  "osDiskId": ids["dealloc_os"], "dataDisks": [{"managedDisk": {"id": ids["dealloc_data"]}}]}
    bill(ids["dealloc_os"], 12.5)
    bill(ids["dealloc_data"], 30.0)                            # → aggregate saving 42.5
    dealloc_nocost = {"id": ids["dealloc_nocost"], "name": "vm-deallocated-nocost",
                      "subscriptionId": "sub-1", "location": "eastus", "vmSize": "Standard_D2s_v3",
                      "powerState": "VM deallocated", "osDiskId": _rid("microsoft.compute/disks", "d-x"),
                      "dataDisks": []}   # no cost → excluded from the aggregate

    # ── inventory buckets ────────────────────────────────────────────────────────────────────────
    inventory = {
        "windows_vms_without_ahb": [{"id": ids["ahb"], "name": "vm-ahb-01", "subscriptionId": "sub-1",
                                     "location": "eastus", "vmSize": "Standard_D16s_v3"}],
        "deallocated_vms": [dealloc_vm, dealloc_nocost],
        "running_vms": inventory_running,
        "unattached_disks": [{"id": ids["disk_orphan"], "name": "disk-orphan-01", "subscriptionId": "sub-1",
                              "resourceGroup": "rg-a", "location": "eastus", "skuName": "Premium_LRS",
                              "diskSizeGB": 128}],
        "orphaned_public_ips": [{"id": ids["pip"], "name": "pip-orphan-01", "subscriptionId": "sub-1",
                                 "resourceGroup": "rg-a", "location": "eastus", "skuName": "Standard"}],
        "orphaned_snapshots": [{"id": ids["snap"], "name": "snap-orphan-01", "subscriptionId": "sub-1",
                                "resourceGroup": "rg-a", "location": "eastus", "diskSizeGB": 200}],
        "idle_nat_gateways": [{"id": ids["nat"], "name": "nat-orphan-01", "subscriptionId": "sub-1",
                               "resourceGroup": "rg-a", "location": "eastus"}],
        "bastion_hosts": [{"id": ids["bastion"], "name": "bastion-01", "subscriptionId": "sub-1",
                           "resourceGroup": "rg-a", "location": "eastus", "skuName": "Standard"}],
        "empty_load_balancers": [{"id": ids["lb"], "name": "lb-review-01", "subscriptionId": "sub-1",
                                  "resourceGroup": "rg-a", "location": "eastus", "skuName": "Standard"}],
        "idle_app_service_plans": [{"id": ids["idle_asp"], "name": "asp-idle-01", "subscriptionId": "sub-1",
                                    "resourceGroup": "rg-a", "location": "eastus", "skuName": "S1"}],
        "paused_sql_databases": [{"id": ids["paused"], "name": "db-paused-01", "status": "Paused"}],
        "stopped_sql_managed_instances": [{"id": ids["stopped_mi"], "name": "mi-stopped-01", "state": "Stopped"}],
        "sql_databases": [
            {"id": ids["sql_ri"], "name": "sql-ri-01", "subscriptionId": "sub-1", "location": "eastus",
             "tier": "GeneralPurpose", "skuName": "GP_Gen5_2"},                         # vCore → eligible
            {"id": ids["sql_dtu"], "name": "sql-dtu-01", "subscriptionId": "sub-1", "location": "eastus",
             "tier": "Standard", "skuName": "Standard"},                                # DTU → NOT eligible
        ],
        "sql_virtual_machines": [],
    }
    bill(ids["ahb"], 300.0)                                    # → AHB = 300 × licence fraction
    bill(ids["disk_orphan"], 19.71)
    bill(ids["pip"], 3.65)
    bill(ids["snap"], 7.40)
    bill(ids["nat"], 32.0)
    bill(ids["bastion"], 138.0)
    bill(ids["idle_asp"], 56.94)
    bill(ids["paused"], 45.0)
    bill(ids["stopped_mi"], 1200.0)
    # empty_load_balancers: intentionally NOT billed → REVIEW (surfaced, zero saving)

    # ── metrics-enriched right-sizing candidates (passed as separate params) ─────────────────────
    active_asps = [{"id": ids["asp_rs"], "name": "asp-rightsize-01", "subscriptionId": "sub-1",
                    "location": "eastus", "skuName": "S3", "max_cpu_pct": 12.0, "max_memory_pct": 15.0,
                    "metric_datapoints": 30, "metric_window_days": 30}]                 # S3→S1 = 170.82
    active_sql_dbs = [{"id": ids["sqldb_rs"], "name": "db-rightsize-01", "subscriptionId": "sub-1",
                       "location": "eastus", "tier": "GeneralPurpose", "skuName": "GP_Gen5", "vcores": 8,
                       "max_cpu_pct": 12.0, "max_data_io_pct": 10.0, "max_log_io_pct": 8.0,
                       "metric_datapoints": 30, "metric_window_days": 30}]              # 8→2 vCore = 600
    bill(ids["sqldb_rs"], 800.0)
    active_sql_mis = [{"id": ids["mi_rs"], "name": "mi-rightsize-01", "subscriptionId": "sub-1",
                       "location": "eastus", "tier": "GeneralPurpose", "skuName": "GP_Gen5", "vcores": 16,
                       "max_cpu_pct": 12.0, "metric_datapoints": 30, "metric_window_days": 30}]  # 16→4 = 1500
    bill(ids["mi_rs"], 2000.0)
    premium_disks = [{"id": ids["disk_rs"], "name": "disk-rightsize-01", "subscriptionId": "sub-1",
                      "location": "eastus", "skuName": "Premium_LRS", "sizeGB": 256, "peak_iops": 120.0,
                      "peak_mbps": 15.0, "metric_datapoints": 30, "metric_window_days": 30}]      # 40-12 = 28

    # ── reservation recommendations (VM + SQL) + a cross-subscription negative control ───────────
    reservation_recs = [
        _vm_ri_group(sku="Standard_F4s_v2", subscription_id="sub-1", p1=100.0, p3=160.0),   # → vm-ri-01
        _vm_ri_group(sku="Standard_F4s_v2", subscription_id="sub-2", p1=100.0, p3=160.0),   # sub-2: no VM → excluded
        _sql_ri_group(sku="SQLDB_Gen5", subscription_id="sub-1", p1=40.0, p3=60.0),         # → sql-ri-01 (vCore)
    ]

    # ── Advisor recs: one real resource-scoped rec (kept) + one subscription-scoped rec (excluded) ─
    advisor_recs = [
        {"id": "ADV-STOR", "properties": {
            "category": "Cost", "impact": "Medium", "impactedField": "Microsoft.Storage/storageAccounts",
            "impactedValue": "stor-advisor-01",
            "shortDescription": {"problem": "Right-size underused storage", "solution": "Move to cool tier"},
            "extendedProperties": {"savingsAmount": "120", "annualSavingsAmount": "1440"},
            "resourceMetadata": {"resourceId": ids["advisor_res"]}}},
        {"id": "ADV-SUB", "properties": {   # subscription-scoped reservation rec → MUST be excluded
            "category": "Cost", "impact": "High", "impactedField": "Microsoft.Subscriptions/subscriptions",
            "impactedValue": "sub-1",
            "shortDescription": {"problem": "Consider virtual machine reserved instance to save over the "
                                 "on-demand costs", "solution": "Buy RI"},
            "extendedProperties": {"annualSavingsAmount": "999"},
            "resourceMetadata": {"resourceId": "/subscriptions/sub-1"}}},
    ]

    engine = FindingsEngine(pricing=MixedPricing(), cost_map=cost_map,
                            advisor_index=build_advisor_index(advisor_recs), currency="USD")
    findings = await pipeline._detect_all(
        engine, inventory, enriched_running, advisor_recs, reservation_recs,
        active_asps=active_asps, active_sql_dbs=active_sql_dbs, active_sql_mis=active_sql_mis,
        premium_disks=premium_disks, assessment_id=999,
    )
    return findings, ids, cost_map


# Expected QUANTIFIED per-category monthly savings (each category is ONE finding here). REVIEW findings
# carry 0. AHB is computed from the real licence fraction.
EXPECTED = {
    "windows_ahb": AHB_EXPECTED,
    "deallocated_vms": 42.5,
    "oversized_vms": 280.32,
    "idle_vms": 70.08,
    "ri_vm": 160.0,
    "sql_db_reserved_capacity": 60.0,
    "unattached_managed_disks": 19.71,
    "orphaned_public_ips": 3.65,
    "orphaned_snapshots": 7.40,
    "idle_nat_gateways": 32.0,
    "bastion_hosts": 138.0,
    "empty_load_balancers": 0.0,                 # REVIEW — surfaced, no counted saving
    "idle_app_service_plans": 56.94,
    "app_service_plan_rightsizing": 170.82,
    "sql_db_rightsizing": 600.0,
    "sql_mi_rightsizing": 1500.0,
    "disk_rightsizing": 28.0,
    "paused_sql_databases": 45.0,
    "stopped_sql_managed_instances": 1200.0,
    "advisor_cost": 120.0,
}


async def test_mixed_pipeline_produces_every_active_category_exactly_once():
    findings, ids, _ = await _run_mixed_environment()
    by_cat: dict = {}
    for f in findings:
        by_cat.setdefault(f["category"], []).append(f)

    # Exactly the expected categories, each exactly once (no missing, no extra, no duplicate category).
    assert set(by_cat) == set(EXPECTED), (
        f"missing={set(EXPECTED) - set(by_cat)} extra={set(by_cat) - set(EXPECTED)}")
    assert all(len(v) == 1 for v in by_cat.values())
    assert len(findings) == len(EXPECTED)


async def test_mixed_pipeline_savings_are_exact_and_source_based():
    findings, ids, _ = await _run_mixed_environment()
    by_cat = {f["category"]: f for f in findings}
    for cat, expected in EXPECTED.items():
        assert by_cat[cat]["estimated_savings_monthly"] == expected, (
            f"{cat}: {by_cat[cat]['estimated_savings_monthly']} != {expected}")


async def test_mixed_pipeline_no_detector_suppresses_another_and_total_equals_sum():
    findings, _, _ = await _run_mixed_environment()
    # No finding was reduced by overlap resolution (unique RI SKU → no VM-compute overlap): the counted
    # saving equals the displayed saving for every finding, so nothing is double-counted or suppressed.
    for f in findings:
        counted = f.get("counted_savings_monthly", f["estimated_savings_monthly"])
        assert counted == f["estimated_savings_monthly"], f"{f['category']} was suppressed/reduced"
    total = round(sum(f["estimated_savings_monthly"] for f in findings), 2)
    assert total == round(sum(EXPECTED.values()), 2)


async def test_mixed_pipeline_affected_resources_resolve_to_real_resources():
    findings, ids, _ = await _run_mixed_environment()
    by_cat = {f["category"]: f for f in findings}

    # VM RI: resolved to the CURRENT VM only (sub-1), never a subscription id, never the sub-2 rec.
    ri = by_cat["ri_vm"]["details"]
    assert ri["affected_count"] == 1
    assert [v["id"] for v in ri["affected_vms"]] == [ids["ri"]]
    assert {v["subscription_id"] for v in ri["affected_vms"]} == {"sub-1"}

    # SQL RI: resolved to the vCore database — NOT the DTU database, NOT a subscription/server id.
    sql = by_cat["sql_db_reserved_capacity"]["details"]
    assert sql["affected_count"] == 1
    assert [d["id"] for d in sql["affected_vms"]] == [ids["sql_ri"]]
    assert ids["sql_dtu"] not in [d["id"] for d in sql["affected_vms"]]
    assert "/databases/" in sql["affected_vms"][0]["id"]

    # Deallocated aggregate: only the VM with a quantifiable disk cost (the no-cost one is excluded).
    assert by_cat["deallocated_vms"]["details"]["affected_count"] == 1

    # Resource-scoped findings carry their real resource id.
    assert by_cat["oversized_vms"]["resource_id"] == ids["rightsize"]
    assert by_cat["idle_vms"]["resource_id"] == ids["idle"]
    assert by_cat["unattached_managed_disks"]["resource_id"] == ids["disk_orphan"]
    assert by_cat["advisor_cost"]["resource_id"] == ids["advisor_res"]

    # Every resource-backed finding points at a real deployed resource (contains '/providers/'), never a
    # bare subscription id; every id is unique (no duplicate finding for the same resource).
    rids = [f["resource_id"] for f in findings if f.get("resource_id")]
    assert all("/providers/" in r for r in rids)
    assert len(rids) == len({r.lower() for r in rids})


async def test_mixed_pipeline_evidence_and_validation_states():
    findings, _, _ = await _run_mixed_environment()
    by_cat = {f["category"]: f for f in findings}
    # Grounded quantified findings are validated against actual billed cost.
    for cat in ("unattached_managed_disks", "orphaned_snapshots", "idle_nat_gateways",
                "paused_sql_databases", "deallocated_vms"):
        assert by_cat[cat]["evidence_state"] == "quantified"
        assert by_cat[cat]["validation_status"] == "validated"
    # The un-billed load balancer is surfaced for REVIEW with no counted saving (never a fabricated one).
    lb = by_cat["empty_load_balancers"]
    assert lb["evidence_state"] == "review"
    assert lb["estimated_savings_monthly"] == 0.0


async def test_mixed_pipeline_negative_controls_produce_no_findings():
    findings, ids, _ = await _run_mixed_environment()
    all_rids = {(f.get("resource_id") or "").lower() for f in findings}

    # Well-utilised running VM → no idle/oversized finding.
    assert ids["healthy"].lower() not in all_rids
    # Deallocated VM with no billable disk cost → not in any finding.
    assert ids["dealloc_nocost"].lower() not in all_rids
    # DTU SQL database → never an affected SQL RI resource and never its own finding.
    assert ids["sql_dtu"].lower() not in all_rids

    # No resource from another subscription leaks in (sub-2 RI rec had no current VM → excluded).
    assert all("/subscriptions/sub-2/" not in (f.get("resource_id") or "") for f in findings)
    ri_details = next(f for f in findings if f["category"] == "ri_vm")["details"]
    assert all(v["subscription_id"] == "sub-1" for v in ri_details["affected_vms"])

    # Subscription-scoped Advisor reservation rec → excluded; no advisor finding uses a subscription id.
    advisor = [f for f in findings if f["category"] == "advisor_cost"]
    assert len(advisor) == 1 and "/providers/" in advisor[0]["resource_id"]
    assert all((f.get("resource_id") or "") not in ("/subscriptions/sub-1", "sub-1") for f in findings)


async def test_mixed_pipeline_overlap_disclosure_flag_without_savings_change():
    # SQL reserved capacity and SQL right-sizing/paused in the SAME subscription are mutually exclusive;
    # the pipeline DISCLOSES this (a flag) but does NOT alter either saving (no fabricated de-overlap).
    findings, _, _ = await _run_mixed_environment()
    by_cat = {f["category"]: f for f in findings}
    assert by_cat["sql_db_rightsizing"]["details"].get("mutually_exclusive_with_reservation")
    assert by_cat["sql_db_rightsizing"]["estimated_savings_monthly"] == 600.0   # unchanged
