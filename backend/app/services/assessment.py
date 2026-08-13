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
    VALIDATED,
    get_cost_map_and_consistency,
    get_runrate_baseline,
    get_service_costs_and_currency,
    linear_growth_rate,
    spend_by_area,
)
from .collection import CollectionReport
from .financial_evidence import REVIEW, counts_toward_total
from .findings import (
    CATEGORY_DISPLAY,
    CONDITIONAL_CATEGORIES,
    ORPHAN_RULES,
    FindingsEngine,
    build_advisor_index,
)
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

# How long to wait out a Cost Management throttle before retrying the per-resource cost-map query once
# (only when the subscription total succeeded but the per-resource detail didn't — see run_assessment).
COST_MAP_RETRY_DELAY_SECONDS = 25

# Findings whose saving is Azure's OWN authoritative number (not our list-price estimate) — kept even
# when per-resource billing is unavailable, since they don't depend on our cost grounding.
_AUTHORITATIVE_CATEGORIES = {"advisor_cost"}


def _withhold_ungrounded(findings: List[Dict]) -> List[Dict]:
    """Drop OUR ungrounded list-price cost findings when per-resource billing is unavailable.

    A grounded finding (saving derived from actual billed cost → `validated`, or carrying an actual cost)
    is kept, as are authoritative Azure-sourced findings (Advisor, reservation recommendations). Only a
    finding whose saving is a pure list-price guess with no billed cost behind it is withheld — those are
    the ones that otherwise show inflated numbers (e.g. a Bastion capped to the whole subscription spend)
    that read as fabricated. Zero-saving informational findings are left untouched.
    """
    kept: List[Dict] = []
    for f in findings:
        monthly = f.get("estimated_savings_monthly", 0) or 0
        grounded = f.get("validation_status") == VALIDATED or f.get("actual_monthly_cost") is not None
        authoritative = (
            f.get("category") in _AUTHORITATIVE_CATEGORIES
            or (f.get("details") or {}).get("source") == "azure_reservation_recommendations"
        )
        if monthly > 0 and not grounded and not authoritative:
            continue  # ungrounded list-price estimate — withhold rather than mislead
        kept.append(f)
    return kept


async def run_assessment(assessment_id: int, subscription_ids: List[str], token: str) -> None:
    db = SessionLocal()
    tracker = ProgressTracker(db, assessment_id)
    try:
        report = CollectionReport(subscriptions_requested=len(subscription_ids))
        # Share ONE RetryStats between the client and the report so every throttle/5xx/exhaustion the
        # retry layer sees (including the reservation path, which absorbs a 429 into a partial result
        # rather than raising) is counted — a retry exhaustion flags the run PARTIAL. Missing ≠ zero.
        client = AzureClient(
            token,
            max_retries=settings.azure_max_retries,
            base_delay=settings.azure_retry_base_delay,
            max_concurrency=settings.azure_max_concurrency,
            stats=report.retry,
        )

        # Every tracker.event() below reports work that has ALREADY happened, with the real numbers
        # from this run — the live event stream renders them verbatim to the client.
        tracker.event("Connected to Azure")
        tracker.event(f"Assessing {len(subscription_ids)} "
                      f"subscription{'s' if len(subscription_ids) != 1 else ''}")

        # 1. Inventory (server-side filtered ARG) — stamps snapshot_at.
        tracker.advance(AssessmentState.FETCHING_RESOURCES)
        tracker.event("Enumerating Azure resources")
        inventory, inv_errors = await collect_inventory(client, subscription_ids)
        _persist_inventory(db, assessment_id, inventory)
        # Record which inventory buckets FAILED (throttle/error after retries) — a failed bucket is NOT
        # "no resources of that type"; it makes the assessment PARTIAL (or FAILED if every bucket failed).
        report.note_inventory(len(inventory), list(inv_errors.keys()))
        if inv_errors:
            logger.warning("Assessment %s inventory partial failures: %s", assessment_id, inv_errors)
        total_resources, type_count, major_types, summary_ok = await _gather_inventory_summary(
            client, subscription_ids)
        report.resources_discovered = total_resources if summary_ok else None
        report.inventory_summary_failed = not summary_ok
        # Publish the scan counts NOW (not just at the end) so the live progress UI can report real
        # discovered totals while the run is still in flight — never a fabricated counter.
        _persist_inventory_summary(db, assessment_id, total_resources, type_count, major_types)
        if total_resources:
            tracker.event(f"Found {total_resources:,} resources across {type_count} types")
        for entry in major_types[:4]:
            tracker.event(f"{entry['count']:,} {entry['type']}")
        # Cosmetic report metadata (client name, subscription names) — best-effort, never fatal.
        await _capture_report_metadata(db, assessment_id, client, subscription_ids, major_types)

        # 2. Utilisation metrics for running VM candidates + active App Service Plans.
        tracker.advance(AssessmentState.FETCHING_METRICS)
        vm_candidates = inventory.get("running_vms", [])
        if vm_candidates:
            tracker.event(f"Sampling 30-day utilisation for {len(vm_candidates)} virtual machines")
        running_vms = await enrich_vms_with_metrics(client, vm_candidates, report=report)
        active_asps = await enrich_asps_with_metrics(
            client, inventory.get("active_app_service_plans", []), report=report)
        active_sql_dbs = await enrich_sql_dbs_with_metrics(
            client, inventory.get("rightsizable_sql_databases", []), report=report)
        active_sql_mis = await enrich_sql_mis_with_metrics(
            client, inventory.get("rightsizable_sql_managed_instances", []), report=report)
        premium_disks = await enrich_disks_with_iops(
            client, inventory.get("rightsizable_premium_disks", []), report=report)

        # 3. Azure Advisor cost recommendations + Azure's own reservation recommendations
        #    (both one call per subscription; the latter is the authoritative RI source).
        tracker.advance(AssessmentState.RUNNING_ADVISOR)
        advisor_recs = await _gather_advisor(client, subscription_ids, report)
        advisor_index = build_advisor_index(advisor_recs)
        tracker.event(f"Azure Advisor returned {len(advisor_recs)} cost recommendations")
        reservation_recs = await _gather_reservation_recs(client, subscription_ids, report)
        if reservation_recs:
            tracker.event(f"Azure reservation engine returned {len(reservation_recs)} recommendations")

        # 4. Actual cost (Cost Management): per-resource for validation, per-service for total spend.
        #    The service query also tells us the subscription's billing currency, which we use to
        #    fetch Azure retail prices in that same currency (so estimates match what the client pays).
        tracker.advance(AssessmentState.CALCULATING_PRICES)
        tracker.event("Reading billed cost from Azure Cost Management")
        # One monthly-history query per sub yields BOTH the last-month cost basis and the
        # month-over-month steadiness signal (half the load on the throttled billing API).
        cost_map, consistency, growth, complete_months, cost_basis, billing_currency = \
            await _gather_cost_and_consistency(client, subscription_ids, report)
        service_costs, currency, spend_estimated, spend_period_days = await _gather_spend_baseline(
            client, subscription_ids, complete_month_exists=complete_months >= 2, report=report)
        # If the SUBSCRIPTION total came through but the PER-RESOURCE detail didn't, Cost Management
        # throttled the heavier query specifically. Wait out the throttle and try the cost map once more
        # before giving up — one grounded retry beats a whole run of ungrounded list-price estimates.
        if not cost_map and service_costs:
            logger.warning("Per-resource billing empty though subscription spend succeeded — retrying "
                           "cost map after %ss backoff.", COST_MAP_RETRY_DELAY_SECONDS)
            await asyncio.sleep(COST_MAP_RETRY_DELAY_SECONDS)
            cost_map, consistency, growth, complete_months, cost_basis, billing_currency = \
                await _gather_cost_and_consistency(client, subscription_ids, report)
        # Per-resource billing is unavailable for this run when the subscription clearly HAS billing
        # (service spend came back) but no per-resource cost did — grounded findings can't be quantified.
        billing_detail_unavailable = (not cost_map) and bool(service_costs)
        report.billing_detail_unavailable = billing_detail_unavailable
        if cost_map:
            tracker.event(f"Matched billed cost for {len(cost_map):,} resources")
        elif billing_detail_unavailable:
            tracker.event("Per-resource billed cost unavailable (Cost Management throttled) — grounded "
                          "findings withheld; re-run for accurate figures")
        else:
            tracker.event("No billed cost returned — estimates will use list pricing")
        # Authoritative billing currency: whatever Cost Management actually returned — the service-cost
        # response first, else the per-resource billing response. Only when NO billing query returned a
        # currency (no billing access at all, so no spend is shown either) do we fall back to the default.
        currency = currency or billing_currency or settings.pricing_currency
        tracker.event(f"Billing currency: {currency}")
        tracker.event("Fetching live Azure retail prices")
        pricing = get_pricing_engine(currency)

        # 5. Detect findings.
        tracker.advance(AssessmentState.DETECTING_FINDINGS)
        snapshot = db.get(Assessment, assessment_id).snapshot_at
        # Total measured spend for the hard cap. Prefer the service-level bill; if that specific query was
        # throttled/empty but we DO have per-resource cost, fall back to the sum of it so the "no finding
        # exceeds spend" guarantee stays active (and matches the spend the UI derives the same way).
        resource_cost_total = round(sum(cost_map.values()), 2) if cost_map else 0.0
        measured_spend = (round(sum(service_costs.values()), 2) if service_costs
                          else (resource_cost_total or None))
        engine = FindingsEngine(
            pricing=pricing,
            cost_map=cost_map,
            advisor_index=advisor_index,
            snapshot_iso=snapshot.isoformat() + "Z" if snapshot else "",
            debug=settings.debug_findings_reasoning,
            reservation_basis=settings.reservation_basis,
            cost_consistency=consistency,
            currency=currency,
            # Absolute ceiling so no single finding can ever exceed the customer's measured spend.
            measured_monthly_spend=measured_spend,
            # Per-resource basis metadata (actual last month / representative / run-rate) so findings can
            # disclose which figure their saving is grounded in and whether it's an estimate.
            cost_basis=cost_basis,
            # Observed billing span (only when there's no complete billing month) — a run-rate saving over
            # too few days can't be defensibly annualised, so those findings become REVIEW downstream.
            billing_span_days=spend_period_days if spend_estimated else None,
        )
        tracker.event("Evaluating reservations, hybrid benefit, right-sizing and waste")
        findings = await _detect_all(
            engine, inventory, running_vms, advisor_recs, reservation_recs, active_asps,
            active_sql_dbs, active_sql_mis, premium_disks
        )
        # When per-resource billing is unavailable, withhold OUR ungrounded list-price findings rather
        # than show numbers that read as fabricated (e.g. a Bastion capped to the whole subscription
        # spend). Grounded and authoritative (Advisor / reservation) findings are kept.
        if billing_detail_unavailable:
            findings = _withhold_ungrounded(findings)
        tracker.event(f"Identified {len(findings)} optimisation "
                      f"{'opportunity' if len(findings) == 1 else 'opportunities'}")

        # 6. Persist findings + roll up totals (incl. actual spend when available).
        tracker.advance(AssessmentState.GENERATING_REPORT)
        tracker.event("Building recommendations")
        # Derive the data-quality state from everything collected. A PARTIAL/FAILED run must NOT read as a
        # clean complete assessment (missing data ≠ zero). NOTE: live-pricing failures are deliberately not
        # a data-quality trigger — they can't fabricate a number: the Batch-1 evidence model already turns
        # an unpriced finding into REVIEW / "Not quantified", so they're safe by construction.
        data_quality = report.data_quality()
        client_note = report.client_message()
        if data_quality != "complete":
            logger.warning("Assessment %s data quality=%s; diagnostics=%s",
                           assessment_id, data_quality, report.diagnostics())
            if client_note:
                tracker.event(client_note)
        _persist_findings_and_totals(
            db, assessment_id, findings, service_costs, total_resources, type_count, currency,
            observed_growth=growth, spend_estimated=spend_estimated, spend_period_days=spend_period_days,
            resource_cost_total=resource_cost_total, billing_detail_unavailable=billing_detail_unavailable,
            data_quality=data_quality, collection_diagnostics=report.diagnostics(),
            data_quality_message=client_note,
        )

        tracker.advance(AssessmentState.COMPLETED)
        tracker.event("Assessment complete")

    except Exception as exc:  # noqa: BLE001 — record failure, never crash the worker
        logger.exception("Assessment %s failed", assessment_id)
        db.rollback()
        tracker.fail(str(exc))
    finally:
        db.close()


async def _gather_advisor(
    client: AzureClient, subscription_ids: List[str], report: Optional[CollectionReport] = None,
) -> List[Dict]:
    results = await asyncio.gather(
        *(client.get_advisor_cost_recommendations(s) for s in subscription_ids),
        return_exceptions=True,
    )
    recs: List[Dict] = []
    for r in results:
        if isinstance(r, list):
            recs.extend(r)
        elif report is not None:
            report.advisor_failed_subs += 1   # a failed sub is NOT "no Advisor recs" — flag it
    return recs


async def _gather_cost_and_consistency(
    client: AzureClient, subscription_ids: List[str], report: Optional[CollectionReport] = None,
):
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
    cost_basis: Dict[str, Dict] = {}   # per-resource basis metadata (label + estimate + variability)
    combined_totals: Dict[str, float] = {}
    detected_currency: str | None = None
    for r in results:
        if isinstance(r, tuple):
            cm, cons, totals, basis, cur = r
            cost_map.update(cm)
            consistency.update(cons)
            cost_basis.update({rid: rep.as_dict() for rid, rep in basis.items()})
            for month, amount in totals.items():
                combined_totals[month] = combined_totals.get(month, 0.0) + amount
            detected_currency = detected_currency or cur
        elif report is not None:
            report.billing_failed_subs += 1    # billing query FAILED for this sub — not "zero spend"
    series = [combined_totals[m] for m in sorted(combined_totals)]
    growth = linear_growth_rate(series)
    # A complete previous billing month exists only if ≥ 2 complete months carried cost — i.e. billing
    # started before the most recent complete month (so that month is a full, representative bill).
    complete_months = sum(1 for amount in combined_totals.values() if amount > 0)
    return cost_map, consistency, growth, complete_months, cost_basis, detected_currency


async def _gather_reservation_recs(
    client: AzureClient, subscription_ids: List[str], report: Optional[CollectionReport] = None,
) -> List[Dict]:
    """Azure's own reservation purchase recommendations, parsed + merged across subscriptions.

    This is the SOLE, authoritative source of Reserved Instance recommendations (VMs and non-VM alike).
    Empty when Cost Management access is unavailable (403/404) or no reservation is worthwhile — in
    which case NO RI recommendation is shown (we never fall back to a retail-estimated discount).
    """
    results = await asyncio.gather(
        *(client.get_reservation_recommendations(s) for s in subscription_ids),
        return_exceptions=True,
    )
    groups: List[Dict] = []
    raw_count = 0
    for sub, r in zip(subscription_ids, results):
        if isinstance(r, list):
            raw_count += len(r)
            groups.extend(parse_reservation_recommendations(r, sub))
        elif report is not None:
            report.reservation_failed_subs += 1   # failed to fetch recs for this sub (≠ none worthwhile)
    if raw_count and not groups:
        logger.warning("Reservation recs: Azure returned %d raw items but the parser kept 0 — "
                       "check resourceType/SKU mapping in reservations.py.", raw_count)
    logger.info("Reservation recs: %d raw items → %d grouped recommendations.", raw_count, len(groups))
    return groups


async def _gather_service_costs(
    client: AzureClient, subscription_ids: List[str], report: Optional[CollectionReport] = None,
):
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
        elif report is not None:
            report.billing_failed_subs += 1    # service-cost query FAILED for this sub — not "zero spend"
    # Almost always one billing currency; if subscriptions report different ones, summing them would
    # be nonsense — surface it loudly and report in the first (don't silently mix).
    if len(currencies) > 1:
        logger.error("Subscriptions report mixed billing currencies %s — totals may be unreliable; "
                     "run one currency at a time.", currencies)
    currency = next(iter(currencies), None)
    return totals, currency


async def _gather_spend_baseline(
    client: AzureClient, subscription_ids: List[str], complete_month_exists: bool,
    report: Optional[CollectionReport] = None,
):
    """The spend baseline: (per_service_costs, currency, estimated, period_days).

    When a complete previous billing month exists → real last-month actuals. Otherwise (new or
    recently-migrated subscription) the last "month" is a partial fragment, so we estimate the monthly
    run rate from the AVERAGE DAILY spend over the observed billing period (first billed day → today).
    `estimated=True` + `period_days` tell the UI to label the spend as an estimate.
    """
    if complete_month_exists:
        totals, currency = await _gather_service_costs(client, subscription_ids, report)
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
        elif isinstance(r, BaseException) and report is not None:
            report.billing_failed_subs += 1   # run-rate query FAILED for this sub — not "zero spend"

    if not totals:  # no daily data either → fall back to whatever last-month/MTD returns
        totals, currency = await _gather_service_costs(client, subscription_ids, report)
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
    # Commitments — Reserved Instances for VMs AND non-VM capacity (SQL/Cosmos/App Service/Files/Disks)
    # all come from Azure's own reservation-recommendations engine: real usage, real prices, real
    # eligibility, only reservable SKUs the customer actually uses. We never compute a discount or
    # recommend a reservation Azure didn't. (The old retail-estimate VM RI detector was retired — the
    # Retail Prices API doesn't publish reservation prices for most VM SKUs.)
    findings += engine.commitments_from_recommendations(reservation_recs)
    # Windows AHB (licence, additive with reservations) — excludes VMs we'd delete (idle/stopped).
    findings += await engine.detect_windows_ahb(
        inventory.get("windows_vms_without_ahb", []), exclude_ids=delete_ids)
    # SQL Server AHB is RETIRED (detect_sql_ahb returns nothing): the SQL licence component can't be
    # reliably derived from any authoritative Microsoft pricing API, so we don't fabricate a figure.
    # Broad-coverage rule-driven orphans (snapshots, empty LBs, NAT gateways, Bastion).
    for bucket in ORPHAN_RULES:
        findings += await engine.detect_orphans(bucket, inventory.get(bucket, []))
    findings += engine.advisor_findings(advisor_recs)
    # Drop noise: a QUANTIFIED finding with no saving (an Advisor rec with no savings figure, or a
    # deallocated VM whose residual disk cost couldn't be quantified) is a zero-value line, not worth
    # showing. REVIEW findings intentionally carry NO saving — they surface a real signal we can't price
    # for this customer — so they are KEPT (the honest alternative to a fabricated number).
    findings = [
        f for f in findings
        if (f.get("estimated_savings_monthly") or 0) > 0 or f.get("evidence_state") == REVIEW
    ]
    # De-overlap RI vs per-VM right-sizing/idle so the total never double-counts the same compute spend,
    # then DISCLOSE the reserved-capacity-vs-right-sizing overlaps we can't numerically de-overlap.
    return flag_reservation_rightsizing_overlaps(resolve_overlaps(_dedupe(findings)))


async def _gather_inventory_summary(client: AzureClient, subscription_ids: List[str]):
    """Return (total_resources, distinct_type_count, major_types, ok) across the subscriptions.

    `major_types` is the top resource types by count as [{"type": short_name, "count": n}] — used
    for the report's Environment Details table (which takes the top 3) and for the live discovery
    metrics on the running-assessment screen. On failure returns (0, 0, [], **False**) so the caller
    records the count as UNKNOWN (not a fabricated zero) and flags the run PARTIAL — a failed count
    query must never read as "this environment has 0 resources".
    """
    try:
        rows = await client.query_resource_graph(subscription_ids, all_resources_summary_query())
    except Exception as exc:  # noqa: BLE001 — a summary failure must not fail the assessment
        logger.warning("Inventory summary query failed: %s", type(exc).__name__)
        return 0, 0, [], False
    total = sum(int(r.get("resourceCount") or 0) for r in rows)
    ranked = sorted(rows, key=lambda r: int(r.get("resourceCount") or 0), reverse=True)
    major = [
        {"type": _friendly_resource_type(r.get("type", "")), "count": int(r.get("resourceCount") or 0)}
        for r in ranked[:8] if int(r.get("resourceCount") or 0) > 0
    ]
    return total, len(rows), major, True


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


# VM-compute recommendations that address the SAME underlying compute spend as a Reserved Instance on
# that VM's current size — you can't both reserve a VM at its current size AND shrink/deallocate it.
_VM_COMPUTE_CATEGORIES = {"idle_vms", "oversized_vms", "vm_rightsizing"}


def resolve_overlaps(findings: List[Dict]) -> List[Dict]:
    """Deterministically de-overlap Reserved Instance savings against per-VM right-sizing/idle savings so
    the same compute spend is never counted twice in the total.

    Azure's RI recommendations are per-(SKU, region) with a quantity (not per-resource-id), so a VM flagged
    for right-sizing/idle can also fall inside an RI recommendation for its current SKU/region. Reserving a
    VM at its current size and shrinking/deallocating it are MUTUALLY EXCLUSIVE strategies for the same
    spend. Precedence rule (reproducible): for each overlapping VM, only the LARGER of the two monthly
    savings is counted toward the total (ties → the concrete per-VM action). The loser's
    `counted_savings_*` is reduced (the RI aggregate is reduced by that instance's per-unit saving; a
    superseded per-VM finding drops to 0). Every finding is still DISPLAYED at its own `estimated_savings_*`
    — only the total uses `counted_savings_*`. No finding is hidden from the UI.
    """
    # Build RI coverage per (sku, region): remaining instances + per-instance monthly saving (in the same
    # term the RI headline uses — 3-year if available, else 1-year).
    coverage: Dict[tuple, Dict] = {}
    for rf in (f for f in findings if f.get("category") == "ri_vm"):
        d = rf.get("details") or {}
        use_3yr = d.get("total_3yr_monthly") is not None
        for item in d.get("reservation_items", []) or []:
            key = ((item.get("sku") or "").lower(), (item.get("region") or "").lower())
            qty = int(item.get("quantity") or 0) or 1
            # Azure's netSavings is the TOTAL saving for the recommended quantity (not per-instance), so
            # the item's saving is pooled as-is and divided by the quantity to get the per-instance rate.
            item_total = item.get("monthly_savings_3yr") if use_3yr else item.get("monthly_savings")
            item_total = item_total if item_total is not None else (item.get("monthly_savings") or 0.0)
            cov = coverage.setdefault(key, {"finding": rf, "save_pool": 0.0, "remaining": 0})
            cov["save_pool"] += (item_total or 0.0)
            cov["remaining"] += qty
    if not coverage:
        return findings  # no RI → nothing to reconcile; counted_savings already == estimated

    for cov in coverage.values():
        cov["per_instance"] = cov["save_pool"] / cov["remaining"] if cov["remaining"] else 0.0

    ri_reduction: Dict[int, list] = {}  # id(ri_finding) -> [ri_finding, total_monthly_reduction]
    for f in findings:
        if f.get("category") not in _VM_COMPUTE_CATEGORIES:
            continue
        d = f.get("details") or {}
        key = ((d.get("vm_sku") or d.get("current_sku") or "").lower(), (d.get("vm_region") or "").lower())
        cov = coverage.get(key)
        if not cov or cov["remaining"] <= 0:
            continue
        cov["remaining"] -= 1  # this VM consumes one reservable instance of the SKU
        per_instance = cov["per_instance"]
        vm_save = f.get("counted_savings_monthly", f.get("estimated_savings_monthly", 0.0)) or 0.0
        if vm_save >= per_instance:
            # Right-sizing/idle wins for this VM — remove this instance's RI saving from the RI's total.
            entry = ri_reduction.setdefault(id(cov["finding"]), [cov["finding"], 0.0])
            entry[1] += per_instance
            f.setdefault("details", {})["overlaps_with_ri"] = True
        else:
            # RI wins — this per-VM compute finding contributes nothing to the total (still displayed).
            f["counted_savings_monthly"] = 0.0
            f["counted_savings_annual"] = 0.0
            f.setdefault("details", {})["overlap_superseded_by_ri"] = True

    for rf, reduction in ri_reduction.values():
        base = rf.get("counted_savings_monthly", rf.get("estimated_savings_monthly", 0.0)) or 0.0
        new_monthly = max(0.0, round(base - reduction, 2))
        rf["counted_savings_monthly"] = new_monthly
        rf["counted_savings_annual"] = round(new_monthly * 12, 2)
        rf.setdefault("details", {})["overlap_reduced_monthly"] = round(reduction, 2)
    return findings


# Reserved-capacity family → the per-resource right-sizing / idle categories that address the SAME
# resources. Reserving a resource's capacity and shrinking/removing it are MUTUALLY EXCLUSIVE strategies,
# but Azure's aggregate reservation recommendations carry no resource id, so — unlike VM RI vs compute,
# which we match on SKU+region and de-overlap numerically in resolve_overlaps — the precise overlap here
# cannot be computed from the data. We refuse to fabricate a de-overlap number (that would violate the
# same no-fabrication rule as fabricating a saving); instead we DISCLOSE the overlap so the UI/PDF make
# clear the two shouldn't simply be summed. Backstops still apply: every finding is capped at measured
# spend, and the dashboard withholds a projected-spend figure whenever savings exceed measured spend.
_RESERVED_RIGHTSIZING_OVERLAP: Dict[str, set] = {
    "sql_db_reserved_capacity": {"sql_db_rightsizing", "paused_sql_databases"},
    "sql_mi_reserved_capacity": {"sql_mi_rightsizing", "stopped_sql_managed_instances"},
    "managed_disk_reserved_capacity": {"disk_rightsizing"},
    "app_service_reserved_capacity": {"app_service_plan_rightsizing", "idle_app_service_plans"},
}


def flag_reservation_rightsizing_overlaps(findings: List[Dict]) -> List[Dict]:
    """Disclosure-only pass (never changes a total): where a reserved-capacity finding and a same-family
    per-resource right-sizing/idle finding co-occur in the same subscription, stamp the per-resource
    finding with `mutually_exclusive_with_reservation` so the UI/PDF disclose that reserving and
    right-sizing the same resource are alternatives, not additive. See `_RESERVED_RIGHTSIZING_OVERLAP`
    for why this is disclosure rather than a numeric de-overlap.
    """
    # Which subscriptions have a reservation finding of each family (blank sub = aggregate/wildcard).
    reserved_subs: Dict[str, set] = {}
    for f in findings:
        cat = f.get("category")
        if cat in _RESERVED_RIGHTSIZING_OVERLAP:
            reserved_subs.setdefault(cat, set()).add(f.get("subscription_id") or "")
    if not reserved_subs:
        return findings

    for f in findings:
        cat = f.get("category")
        fsub = f.get("subscription_id") or ""
        for reserved_cat, consumers in _RESERVED_RIGHTSIZING_OVERLAP.items():
            subs = reserved_subs.get(reserved_cat)
            if not subs or cat not in consumers:
                continue
            # Co-occur when the two are in the same subscription, or either side's subscription is unknown
            # (an aggregate reservation with no sub) — the conservative, disclose-don't-miss choice.
            if fsub in subs or "" in subs or fsub == "":
                f.setdefault("details", {})["mutually_exclusive_with_reservation"] = \
                    CATEGORY_DISPLAY.get(reserved_cat, reserved_cat)
    return findings


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


def _persist_inventory_summary(
    db, assessment_id: int, total: int, type_count: int, major_types: List[Dict] | None = None,
) -> None:
    """Store the scan counts as soon as inventory is collected.

    The same values are re-stamped by `_persist_findings_and_totals` at the end; writing them here
    too means the polling frontend can show REAL discovered counts mid-run instead of waiting for
    the whole pipeline (or, worse, inventing a number).
    """
    assessment = db.get(Assessment, assessment_id)
    if assessment is None:
        return
    assessment.total_resources = total
    assessment.resource_type_count = type_count
    if major_types:
        assessment.major_resource_types = major_types
    db.commit()


def _persist_findings_and_totals(
    db, assessment_id: int, findings: List[Dict], service_costs: Dict[str, float] | None = None,
    total_resources: int = 0, type_count: int = 0, currency: str | None = None,
    observed_growth: float | None = None, spend_estimated: bool = False,
    spend_period_days: int | None = None, resource_cost_total: float | None = None,
    billing_detail_unavailable: bool = False,
    data_quality: str = "complete", collection_diagnostics: Dict | None = None,
    data_quality_message: str | None = None,
) -> None:
    total_monthly = 0.0
    total_annual = 0.0
    needs_review = 0
    for fd in findings:
        row = {k: v for k, v in fd.items() if k in _FINDING_COLUMNS}
        db.add(Finding(assessment_id=assessment_id, **row))
        # The headline total counts ONLY quantified, non-conditional savings. REVIEW findings (no
        # defensible figure) and conditional Azure Hybrid Benefit (realised only with eligible licences)
        # are surfaced separately and must never inflate the headline. `_dedupe` upstream already ensures
        # at most one finding per resource, so this sum is non-overlapping by construction.
        conditional = fd.get("category") in CONDITIONAL_CATEGORIES
        if counts_toward_total(fd.get("evidence_state", "quantified"), conditional):
            # Use the NON-OVERLAPPING contribution (counted_savings), not the finding's own displayed
            # value, so RI + right-sizing on the same VM never double-count.
            total_monthly += fd.get("counted_savings_monthly", fd.get("estimated_savings_monthly", 0))
            total_annual += fd.get("counted_savings_annual", fd.get("estimated_savings_annual", 0))
        if fd.get("validation_status") == NEEDS_REVIEW:
            needs_review += 1

    assessment = db.get(Assessment, assessment_id)
    assessment.total_savings_monthly = round(total_monthly, 2)
    assessment.total_savings_annual = round(total_annual, 2)
    assessment.currency = (currency or "USD").upper()
    assessment.observed_annual_growth = observed_growth
    assessment.findings_count = len(findings)
    assessment.needs_review_count = needs_review
    assessment.billing_detail_unavailable = 1 if billing_detail_unavailable else 0
    # Data-quality state (complete/partial/failed) + the concise client message + detailed diagnostics.
    # A PARTIAL/FAILED run is recorded as such so it never presents as a clean complete assessment.
    assessment.data_quality = data_quality
    assessment.data_quality_message = data_quality_message
    assessment.collection_diagnostics = collection_diagnostics
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
    elif resource_cost_total and resource_cost_total > 0:
        # The service-level spend query returned nothing (a common transient Cost Management throttle),
        # but we DO have per-resource billed cost (the same basis the findings are grounded in). Derive
        # the current spend from that so the KPI + projected spend are shown and stay consistent with the
        # findings — flagged as an estimate rather than left blank as "Awaiting billing data".
        monthly = round(resource_cost_total, 2)
        assessment.current_monthly_spend = monthly
        assessment.current_annual_spend = round(monthly * 12, 2)
        assessment.cost_data_available = 1
        assessment.spend_estimated = 1   # derived from per-resource cost, not a full service-level bill
        assessment.spend_period_days = spend_period_days
    else:
        assessment.cost_data_available = 0
        assessment.spend_estimated = 0

    db.commit()
