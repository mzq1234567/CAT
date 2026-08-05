"""
Assessment orchestrator (rewired in Steps 5–6).

Drives the state machine through the full pipeline, wiring together:
  server-side-filtered inventory (Step 3) → utilisation metrics (Step 6) → Advisor (existing)
  → live pricing (Step 2) → actual-cost validation (Step 4) → findings engine (Step 6).

Runs as a FastAPI BackgroundTask with its own DB session. Progress + the "as of" snapshot time
are persisted so the frontend can show real progress and reports never silently go stale.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, List

from ..config import settings
from ..database import SessionLocal
from ..models.db import Assessment, Finding, InventoryItem
from .azure_client import AzureClient
from .cost_management import (
    NEEDS_REVIEW,
    get_cost_map_and_consistency,
    get_runrate_baseline,
    get_service_costs_and_currency,
    linear_growth_rate,
    spend_by_area,
)
from .findings import ORPHAN_RULES, FindingsEngine, build_advisor_index
from .inventory import collect_inventory
from .kql import all_resources_summary_query
from .metrics import (
    enrich_asps_with_metrics,
    enrich_disks_with_iops,
    enrich_sql_dbs_with_metrics,
    enrich_sql_mis_with_metrics,
    enrich_vms_with_metrics,
)
from .pricing import get_pricing_engine
from .reservations import parse_reservation_recommendations
from .state_machine import AssessmentState, ProgressTracker

logger = logging.getLogger("cat.assessment")

# Finding-model column names — used to strip engine-only keys before persisting.
_FINDING_COLUMNS = {c.name for c in Finding.__table__.columns} - {"id", "assessment_id"}


async def run_assessment(assessment_id: int, subscription_ids: List[str], token: str) -> None:
    db = SessionLocal()
    tracker = ProgressTracker(db, assessment_id)
    try:
        client = AzureClient(
            token,
            max_retries=settings.azure_max_retries,
            base_delay=settings.azure_retry_base_delay,
        )

        # 1. Inventory (server-side filtered ARG) — stamps snapshot_at.
        tracker.advance(AssessmentState.FETCHING_RESOURCES)
        inventory, inv_errors = await collect_inventory(client, subscription_ids)
        _persist_inventory(db, assessment_id, inventory)
        if inv_errors:
            logger.warning("Assessment %s inventory partial failures: %s", assessment_id, inv_errors)
        total_resources, type_count, major_types = await _gather_inventory_summary(
            client, subscription_ids)
        # Cosmetic report metadata (client name, subscription names) — best-effort, never fatal.
        await _capture_report_metadata(db, assessment_id, client, subscription_ids, major_types)

        # 2. Utilisation metrics for running VM candidates + active App Service Plans.
        tracker.advance(AssessmentState.FETCHING_METRICS)
        running_vms = await enrich_vms_with_metrics(client, inventory.get("running_vms", []))
        active_asps = await enrich_asps_with_metrics(client, inventory.get("active_app_service_plans", []))
        active_sql_dbs = await enrich_sql_dbs_with_metrics(
            client, inventory.get("rightsizable_sql_databases", []))
        active_sql_mis = await enrich_sql_mis_with_metrics(
            client, inventory.get("rightsizable_sql_managed_instances", []))
        premium_disks = await enrich_disks_with_iops(
            client, inventory.get("rightsizable_premium_disks", []))

        # 3. Azure Advisor cost recommendations + Azure's own reservation recommendations
        #    (both one call per subscription; the latter is the authoritative RI source).
        tracker.advance(AssessmentState.RUNNING_ADVISOR)
        advisor_recs = await _gather_advisor(client, subscription_ids)
        advisor_index = build_advisor_index(advisor_recs)
        reservation_recs = await _gather_reservation_recs(client, subscription_ids)

        # 4. Actual cost (Cost Management): per-resource for validation, per-service for total spend.
        #    The service query also tells us the subscription's billing currency, which we use to
        #    fetch Azure retail prices in that same currency (so estimates match what the client pays).
        tracker.advance(AssessmentState.CALCULATING_PRICES)
        # One monthly-history query per sub yields BOTH the last-month cost basis and the
        # month-over-month steadiness signal (half the load on the throttled billing API).
        cost_map, consistency, growth, complete_months = await _gather_cost_and_consistency(
            client, subscription_ids)
        service_costs, currency, spend_estimated, spend_period_days = await _gather_spend_baseline(
            client, subscription_ids, complete_month_exists=complete_months >= 2)
        currency = currency or settings.pricing_currency
        pricing = get_pricing_engine(currency)

        # 5. Detect findings.
        tracker.advance(AssessmentState.DETECTING_FINDINGS)
        snapshot = db.get(Assessment, assessment_id).snapshot_at
        engine = FindingsEngine(
            pricing=pricing,
            cost_map=cost_map,
            advisor_index=advisor_index,
            snapshot_iso=snapshot.isoformat() + "Z" if snapshot else "",
            debug=settings.debug_findings_reasoning,
            reservation_basis=settings.reservation_basis,
            cost_consistency=consistency,
            currency=currency,
        )
        findings = await _detect_all(
            engine, inventory, running_vms, advisor_recs, reservation_recs, active_asps,
            active_sql_dbs, active_sql_mis, premium_disks
        )

        # 6. Persist findings + roll up totals (incl. actual spend when available).
        tracker.advance(AssessmentState.GENERATING_REPORT)
        _persist_findings_and_totals(
            db, assessment_id, findings, service_costs, total_resources, type_count, currency,
            observed_growth=growth, spend_estimated=spend_estimated, spend_period_days=spend_period_days,
        )

        tracker.advance(AssessmentState.COMPLETED)

    except Exception as exc:  # noqa: BLE001 — record failure, never crash the worker
        logger.exception("Assessment %s failed", assessment_id)
        db.rollback()
        tracker.fail(str(exc))
    finally:
        db.close()


async def _gather_advisor(client: AzureClient, subscription_ids: List[str]) -> List[Dict]:
    results = await asyncio.gather(
        *(client.get_advisor_cost_recommendations(s) for s in subscription_ids),
        return_exceptions=True,
    )
    recs: List[Dict] = []
    for r in results:
        if isinstance(r, list):
            recs.extend(r)
    return recs


async def _gather_cost_and_consistency(client: AzureClient, subscription_ids: List[str]):
    """Merge (cost_map, consistency, growth) across subscriptions from one monthly-history query each.

    `growth` is the environment's annual spend-growth rate from a best-fit line through the last
    ~6 complete months (totals summed across all subscriptions, months aligned). None when there's
    too little history to trend. Powers the report's Linear/Conservative growth projections.
    """
    results = await asyncio.gather(
        *(get_cost_map_and_consistency(client, s, months=6) for s in subscription_ids),
        return_exceptions=True,
    )
    cost_map: Dict[str, float] = {}
    consistency: Dict[str, Dict] = {}
    combined_totals: Dict[str, float] = {}
    for r in results:
        if isinstance(r, tuple):
            cm, cons, totals = r
            cost_map.update(cm)
            consistency.update(cons)
            for month, amount in totals.items():
                combined_totals[month] = combined_totals.get(month, 0.0) + amount
    series = [combined_totals[m] for m in sorted(combined_totals)]
    growth = linear_growth_rate(series)
    # A complete previous billing month exists only if ≥ 2 complete months carried cost — i.e. billing
    # started before the most recent complete month (so that month is a full, representative bill).
    complete_months = sum(1 for amount in combined_totals.values() if amount > 0)
    return cost_map, consistency, growth, complete_months


async def _gather_reservation_recs(client: AzureClient, subscription_ids: List[str]) -> List[Dict]:
    """Azure's own reservation purchase recommendations, parsed + merged across subscriptions.

    Empty when Cost Management access is unavailable (403/404) or no reservation is worthwhile —
    the findings engine then falls back to the retail-estimate commitment detector.
    """
    results = await asyncio.gather(
        *(client.get_reservation_recommendations(s) for s in subscription_ids),
        return_exceptions=True,
    )
    groups: List[Dict] = []
    raw_count = 0
    for sub, r in zip(subscription_ids, results):
        if isinstance(r, list) and r:
            raw_count += len(r)
            groups.extend(parse_reservation_recommendations(r, sub))
    if raw_count and not groups:
        logger.warning("Reservation recs: Azure returned %d raw items but the parser kept 0 — "
                       "check resourceType/SKU mapping in reservations.py.", raw_count)
    logger.info("Reservation recs: %d raw items → %d grouped recommendations.", raw_count, len(groups))
    return groups


async def _gather_service_costs(client: AzureClient, subscription_ids: List[str]):
    """Sum actual per-service spend across subscriptions + detect billing currency.

    Returns (totals, currency). Empty totals / None currency when billing access is unavailable.
    """
    results = await asyncio.gather(
        *(get_service_costs_and_currency(client, s) for s in subscription_ids),
        return_exceptions=True,
    )
    totals: Dict[str, float] = {}
    currencies: set = set()
    for r in results:
        if isinstance(r, tuple):
            svc, cur = r
            for service, cost in svc.items():
                totals[service] = totals.get(service, 0.0) + cost
            if cur:
                currencies.add(cur)
    # Almost always one billing currency; if subscriptions report different ones, summing them would
    # be nonsense — surface it loudly and report in the first (don't silently mix).
    if len(currencies) > 1:
        logger.error("Subscriptions report mixed billing currencies %s — totals may be unreliable; "
                     "run one currency at a time.", currencies)
    currency = next(iter(currencies), None)
    return totals, currency


async def _gather_spend_baseline(
    client: AzureClient, subscription_ids: List[str], complete_month_exists: bool
):
    """The spend baseline: (per_service_costs, currency, estimated, period_days).

    When a complete previous billing month exists → real last-month actuals. Otherwise (new or
    recently-migrated subscription) the last "month" is a partial fragment, so we estimate the monthly
    run rate from the AVERAGE DAILY spend over the observed billing period (first billed day → today).
    `estimated=True` + `period_days` tell the UI to label the spend as an estimate.
    """
    if complete_month_exists:
        totals, currency = await _gather_service_costs(client, subscription_ids)
        return totals, currency, False, None

    results = await asyncio.gather(
        *(get_runrate_baseline(client, s) for s in subscription_ids), return_exceptions=True
    )
    totals: Dict[str, float] = {}
    currency: str | None = None
    period_days = 0
    for r in results:
        if isinstance(r, dict):
            for service, cost in r["service_costs"].items():
                totals[service] = totals.get(service, 0.0) + cost
            currency = currency or r.get("currency")
            period_days = max(period_days, int(r.get("period_days") or 0))

    if not totals:  # no daily data either → fall back to whatever last-month/MTD returns
        totals, currency = await _gather_service_costs(client, subscription_ids)
        return totals, currency, False, None
    logger.info("Spend baseline: no complete billing month → estimated run rate over %d days.", period_days)
    return totals, currency, True, (period_days or None)


async def _detect_all(engine, inventory, running_vms, advisor_recs, reservation_recs=None,
                      active_asps=None, active_sql_dbs=None, active_sql_mis=None,
                      premium_disks=None) -> List[Dict]:
    reservation_recs = reservation_recs or []
    active_asps = active_asps or []
    active_sql_dbs = active_sql_dbs or []
    active_sql_mis = active_sql_mis or []
    premium_disks = premium_disks or []
    findings: List[Dict] = []
    findings += await engine.detect_unattached_disks(inventory.get("unattached_disks", []))
    findings += await engine.detect_orphaned_public_ips(inventory.get("orphaned_public_ips", []))
    findings += await engine.detect_idle_app_service_plans(inventory.get("idle_app_service_plans", []))
    findings += await engine.detect_app_service_rightsizing(active_asps)
    findings += await engine.detect_sql_db_rightsizing(active_sql_dbs)
    findings += await engine.detect_sql_mi_rightsizing(active_sql_mis)
    sql_vm_ids = {(r.get("vmId") or "").lower()
                  for r in inventory.get("sql_virtual_machines", []) if r.get("vmId")}
    findings += await engine.detect_disk_rightsizing(premium_disks, exclude_vm_ids=sql_vm_ids)
    findings += engine.detect_deallocated_vms(inventory.get("deallocated_vms", []))
    findings += engine.detect_paused_sql_databases(inventory.get("paused_sql_databases", []))
    findings += engine.detect_stopped_sql_managed_instances(
        inventory.get("stopped_sql_managed_instances", []))
    util_findings = await engine.detect_vm_utilisation_findings(running_vms)
    findings += util_findings
    # VMs we'd DELETE (idle) or that are already stopped must not also earn an AHB licence saving —
    # you can't save a licence on a VM you're removing. Collect those to exclude from AHB below.
    delete_ids = {(f.get("resource_id") or "").lower()
                  for f in util_findings if f.get("category") == "idle_vms"}
    delete_ids |= {(vm.get("id") or "").lower()
                   for vm in inventory.get("deallocated_vms", []) if vm.get("id")}
    # Commitments. Non-VM reserved capacity (SQL/Cosmos/App Service/Files/Disks) comes from Azure's own
    # reservation engine (real usage + real prices). VMs are handled separately by detect_vm_commitments,
    # which recommends Reserved Instances for PRODUCTION VMs (Savings Plans removed).
    findings += engine.commitments_from_recommendations(reservation_recs)
    # VMs still paying the Windows licence (not on AHB) → the RI base must exclude that licence so RI and
    # AHB savings don't overlap on the same VM.
    win_ids = {vm["id"].lower() for vm in inventory.get("windows_vms_without_ahb", []) if vm.get("id")}
    findings += await engine.detect_vm_commitments(running_vms, win_ids)
    # Windows AHB (licence, additive with reservations) — excludes VMs we'd delete (idle/stopped).
    findings += await engine.detect_windows_ahb(
        inventory.get("windows_vms_without_ahb", []), exclude_ids=delete_ids)
    # SQL Server AHB — same idea for vCore SQL DB/MI; exclude paused/stopped SQL (no billable compute).
    sql_delete_ids = {(db.get("id") or "").lower()
                      for db in inventory.get("paused_sql_databases", []) if db.get("id")}
    sql_delete_ids |= {(mi.get("id") or "").lower()
                       for mi in inventory.get("stopped_sql_managed_instances", []) if mi.get("id")}
    findings += await engine.detect_sql_ahb(
        inventory.get("sql_ahb_eligible", []), exclude_ids=sql_delete_ids)
    # Broad-coverage rule-driven orphans (snapshots, empty LBs, NAT gw, GRS vaults…).
    for bucket in ORPHAN_RULES:
        findings += await engine.detect_orphans(bucket, inventory.get(bucket, []))
    findings += engine.advisor_findings(advisor_recs)
    # A cost report lists opportunities worth acting on — drop any finding with no quantified saving.
    # Azure Advisor returns some "cost" recs without a savings figure (e.g. "disable Front Door health
    # probes"), and a deallocated VM's residual disk cost isn't quantified here; both surface as ₹0.
    # A zero-value line is noise in a client deliverable, so it's filtered out before dedupe/persist.
    findings = [f for f in findings if (f.get("estimated_savings_monthly") or 0) > 0]
    return _dedupe(findings)


async def _gather_inventory_summary(client: AzureClient, subscription_ids: List[str]):
    """Return (total_resources, distinct_type_count, major_types) across the subscriptions.

    `major_types` is the top resource types by count as [{"type": short_name, "count": n}] — used
    for the report's Environment Details table. (0, 0, []) on failure.
    """
    try:
        rows = await client.query_resource_graph(subscription_ids, all_resources_summary_query())
    except Exception as exc:  # noqa: BLE001 — a summary failure must not fail the assessment
        logger.warning("Inventory summary query failed: %s", exc)
        return 0, 0, []
    total = sum(int(r.get("resourceCount") or 0) for r in rows)
    ranked = sorted(rows, key=lambda r: int(r.get("resourceCount") or 0), reverse=True)
    major = [
        {"type": _friendly_resource_type(r.get("type", "")), "count": int(r.get("resourceCount") or 0)}
        for r in ranked[:3] if int(r.get("resourceCount") or 0) > 0
    ]
    return total, len(rows), major


# ARM type → friendly name for the report (e.g. microsoft.compute/virtualmachines → Virtual Machines).
_FRIENDLY_TYPES = {
    "microsoft.compute/virtualmachines": "Virtual Machines",
    "microsoft.compute/disks": "Disks",
    "microsoft.compute/snapshots": "Snapshots",
    "microsoft.network/networkinterfaces": "Network Interfaces",
    "microsoft.network/publicipaddresses": "Public IP Addresses",
    "microsoft.network/networksecuritygroups": "Network Security Groups",
    "microsoft.network/virtualnetworks": "Virtual Networks",
    "microsoft.network/virtualnetworkgateways": "Virtual Network Gateways",
    "microsoft.storage/storageaccounts": "Storage Accounts",
    "microsoft.sql/servers/databases": "SQL Databases",
    "microsoft.web/serverfarms": "App Service Plans",
    "microsoft.web/sites": "App Services",
}


def _friendly_resource_type(arm_type: str) -> str:
    t = (arm_type or "").lower()
    if t in _FRIENDLY_TYPES:
        return _FRIENDLY_TYPES[t]
    # Fall back to the last path segment, title-cased (e.g. .../bastionhosts → Bastionhosts).
    tail = t.split("/")[-1] if t else "Resources"
    return tail.replace("_", " ").title() or "Resources"


async def _capture_report_metadata(db, assessment_id, client, subscription_ids, major_types):
    """Persist client name (tenant display name) + subscription names + major types for the report."""
    assessment = db.get(Assessment, assessment_id)
    if assessment is None:
        return
    try:
        assessment.tenant_display_name = await client.get_tenant_display_name(assessment.tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Tenant name lookup failed: %s", exc)
    try:
        subs = await client.get_subscriptions()
        wanted = set(subscription_ids)
        assessment.subscription_names = {
            s.get("subscriptionId"): s.get("displayName")
            for s in subs if s.get("subscriptionId") in wanted
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("Subscription name lookup failed: %s", exc)
    if major_types:
        assessment.major_resource_types = major_types
    db.commit()


def _dedupe(findings: List[Dict]) -> List[Dict]:
    """At most one finding per resource.

    You implement a single optimization per resource (you don't both shut a VM down *and* buy it a
    reservation), so summing several findings on the same resource over-counts savings. Keep the
    highest-savings finding for each resource id; findings without a resource id pass through.
    """
    best_by_resource: Dict[str, Dict] = {}
    passthrough: List[Dict] = []
    for f in findings:
        rid = (f.get("resource_id") or "").lower()
        if not rid:
            passthrough.append(f)
            continue
        current = best_by_resource.get(rid)
        if current is None or f["estimated_savings_annual"] > current["estimated_savings_annual"]:
            best_by_resource[rid] = f
    return passthrough + list(best_by_resource.values())


def _persist_inventory(db, assessment_id: int, inventory: Dict[str, List[Dict]]) -> None:
    for bucket, items in inventory.items():
        for item in items:
            db.add(InventoryItem(
                assessment_id=assessment_id,
                subscription_id=item.get("subscriptionId", ""),
                resource_id=item.get("id", ""),
                resource_type=bucket,
                resource_name=item.get("name", ""),
                location=item.get("location"),
                resource_group=item.get("resourceGroup"),
                data=item,
            ))
    db.commit()


def _persist_findings_and_totals(
    db, assessment_id: int, findings: List[Dict], service_costs: Dict[str, float] | None = None,
    total_resources: int = 0, type_count: int = 0, currency: str | None = None,
    observed_growth: float | None = None, spend_estimated: bool = False,
    spend_period_days: int | None = None,
) -> None:
    total_monthly = 0.0
    total_annual = 0.0
    needs_review = 0
    for fd in findings:
        row = {k: v for k, v in fd.items() if k in _FINDING_COLUMNS}
        db.add(Finding(assessment_id=assessment_id, **row))
        # All identified savings count toward the headline (incl. Azure Hybrid Benefit); the UI carries
        # an info note that savings may include AHB where applicable.
        total_monthly += fd.get("estimated_savings_monthly", 0)
        total_annual += fd.get("estimated_savings_annual", 0)
        if fd.get("validation_status") == NEEDS_REVIEW:
            needs_review += 1

    assessment = db.get(Assessment, assessment_id)
    assessment.total_savings_monthly = round(total_monthly, 2)
    assessment.total_savings_annual = round(total_annual, 2)
    assessment.currency = (currency or "USD").upper()
    assessment.observed_annual_growth = observed_growth
    assessment.findings_count = len(findings)
    assessment.needs_review_count = needs_review
    if total_resources:
        assessment.total_resources = total_resources
        assessment.resource_type_count = type_count

    # Actual spend — only set when Cost Management returned data (billing access present).
    if service_costs:
        monthly = round(sum(service_costs.values()), 2)
        assessment.current_monthly_spend = monthly
        assessment.current_annual_spend = round(monthly * 12, 2)
        assessment.spend_by_area = spend_by_area(service_costs)
        assessment.cost_data_available = 1
        # Flag when the baseline is an estimated run rate (no complete billing month) so the UI says so.
        assessment.spend_estimated = 1 if spend_estimated else 0
        assessment.spend_period_days = spend_period_days
        # No clamp: every finding is grounded in the resource's actual cost, so the sum is already
        # ≤ spend by construction. If it somehow isn't, that's a real bug to surface — not to paper
        # over by forcing savings == spend (which reads as a nonsensical 100% reduction).
        if assessment.total_savings_annual > assessment.current_annual_spend > 0:
            logger.error("Assessment %s: savings (%.0f) exceed spend (%.0f) despite grounding — "
                         "investigate.", assessment_id, assessment.total_savings_annual,
                         assessment.current_annual_spend)
    else:
        assessment.cost_data_available = 0
        assessment.spend_estimated = 0

    db.commit()
