"""
Parse Azure Consumption `reservationRecommendations` into commitment-finding input.

This is the *authoritative* reservation source: Azure's engine simulates the customer's actual
hourly usage over the look-back window at their real (negotiated) prices, excludes reservations
already owned, and returns the quantity that maximises savings — per SKU/region, for 1-year and
3-year terms, across VMs, SQL, Cosmos, MySQL/PostgreSQL, App Service, Managed Disk and more.

We keep the 30-day look-back (a monthly run-rate), normalise every figure to a monthly number
(`× 30 / lookBackDays`, robust to whichever window Azure returns), and group the 1-year vs 3-year
terms for the same (resource-type, SKU, region, scope) so the UI can compare them side by side.

Two response shapes exist and are both handled:
  * legacy  — flat decimals (`netSavings: 0.58`), returned at subscription scope;
  * modern  — money objects (`netSavings: {currency, value}`), returned at billing scope.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Consumption resourceType (lower) → (our finding category, human product name). The category drives
# the executive "savings by area" grouping (see frontend area.ts) + the display label.
_RT_META: Dict[str, tuple] = {
    "virtualmachines": ("ri_vm", "Virtual Machines"),
    "sqldatabases": ("sql_db_reserved_capacity", "SQL Database"),
    "sqldatawarehouse": ("sql_db_reserved_capacity", "SQL Data Warehouse"),
    "sqlmanagedinstance": ("sql_mi_reserved_capacity", "SQL Managed Instance"),
    "manageddisk": ("managed_disk_reserved_capacity", "Managed Disks"),
    "mysql": ("mysql_reserved_capacity", "Azure Database for MySQL"),
    "postgresql": ("mysql_reserved_capacity", "Azure Database for PostgreSQL"),
    "mariadb": ("mysql_reserved_capacity", "Azure Database for MariaDB"),
    "cosmosdb": ("cosmos_reserved_capacity", "Cosmos DB"),
    "rediscache": ("cosmos_reserved_capacity", "Azure Cache for Redis"),
    "appservice": ("app_service_reserved_capacity", "App Service"),
    "blockblob": ("azure_files_reserved_capacity", "Blob Storage"),
}

# Azure may return 7-, 30- and 60-day variants for the same SKU. Keep one per term, preferring the
# 30-day window (a natural monthly run-rate), then 60, then 7 — every figure normalised to monthly.
_LOOKBACK_PREFERENCE = {30: 3, 60: 2, 7: 1}


def _amount(v: Any) -> float:
    """A recommendation figure is either a flat number (legacy) or {currency, value} (modern)."""
    if isinstance(v, dict):
        return float(v.get("value") or 0)
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _lookback_days(v: Any) -> int:
    if isinstance(v, (int, float)):
        return int(v)
    return {"Last7Days": 7, "Last30Days": 30, "Last60Days": 60}.get(str(v or ""), 30)


def _sku_of(props: Dict, item: Optional[Dict] = None) -> Optional[str]:
    """The recommended SKU. VM recs carry `normalizedSize`; non-VM recs (SQL/Cosmos/App Service/...) may
    instead deliver it as a top-level `skuName`/`sku` or inside `skuProperties`. We try all of them so a
    non-VM recommendation is never silently dropped for lack of `normalizedSize` (which would suppress a
    legitimate SQL reservation). Order: normalizedSize → skuName → sku → skuProperties → flexibility group.
    """
    sku = props.get("normalizedSize") or props.get("skuName") or props.get("sku")
    if not sku and item:
        sku = item.get("sku") or item.get("skuName")
    if sku:
        return sku
    for kv in props.get("skuProperties") or []:
        if str(kv.get("name", "")).lower() in ("skuname", "name"):
            return kv.get("value")
    return props.get("instanceFlexibilityGroup")


def _resource_type(props: Dict, sku: Optional[str]) -> str:
    rt = (props.get("resourceType") or "").lower()
    if rt:
        return rt
    # Legacy VM items sometimes omit resourceType — infer from a Standard_* SKU.
    if sku and str(sku).lower().startswith("standard_"):
        return "virtualmachines"
    return rt


def parse_reservation_recommendations(
    items: List[Dict], subscription_id: str = "",
) -> List[Dict]:
    """Group raw recommendation items into per-(type, SKU, region, scope) groups holding both terms.

    Returns a list of dicts:
      {resource_type, category, product, sku, region, scope, flexibility_group, subscription_id,
       terms: {'P1Y': {monthly_savings, monthly_ondemand, monthly_reserved, quantity}, 'P3Y': {...}}}
    Only groups with a positive saving on at least one term are returned.
    """
    groups: Dict[tuple, Dict] = {}
    for it in items:
        props = it.get("properties", {}) or {}
        sku = _sku_of(props, it)
        rtype = _resource_type(props, sku)
        meta = _RT_META.get(rtype)
        raw_rtype = props.get("resourceType")
        # PRE-DROP trace: EVERY raw recommendation, BEFORE any classification drop — so a real run shows
        # whether Azure returned a SQL (or any) recommendation and, if so, exactly how it maps. This is
        # deliberately before the drop below: a SQL rec whose resourceType/SKU we fail to recognise would
        # otherwise vanish silently (the "our parser is dropping them" case).
        logger.info(
            "RI raw item: resourceType=%r -> category=%s sku=%r (normalizedSize=%r skuName=%r sku=%r "
            "flexGroup=%r) term=%s scope=%s netSavings=%s",
            raw_rtype, (meta[0] if meta else None), sku, props.get("normalizedSize"),
            props.get("skuName"), props.get("sku") or it.get("sku"),
            props.get("instanceFlexibilityGroup"), props.get("term"), props.get("scope"),
            props.get("netSavings"),
        )
        if meta is None or not sku:
            logger.info("RI raw item DROPPED at classification: resourceType=%r sku=%r reason=%s",
                        raw_rtype, sku, "unrecognized_resourceType" if meta is None else "no_sku")
            continue
        days = _lookback_days(props.get("lookBackPeriod")) or 30
        factor = 30.0 / days  # normalise the window's total to a monthly run-rate
        raw_savings = _amount(props.get("netSavings"))
        raw_ondemand = _amount(props.get("costWithNoReservedInstances"))
        raw_reserved = _amount(props.get("totalCostWithReservedInstances"))
        savings = round(raw_savings * factor, 2)

        category, product = meta
        region = it.get("location") or props.get("location") or ""
        term = props.get("term") or "P1Y"
        scope = props.get("scope") or "Single"
        # Raw-response trace: the EXACT Azure figures (per the 2023-05-01 schema — netSavings is the
        # total estimated saving for the look-back window; costWith/NoReservedInstances are the window's
        # totals) alongside the monthly-normalised value and the factor, so a real run can be checked
        # end-to-end (no incorrect annualisation, right term/currency). Prices/usage only, no secrets.
        logger.info(
            "RI raw rec: type=%s sku=%s region=%s term=%s lookBack=%dd qty=%s meterId=%s | "
            "netSavings=%.2f costNoRI=%.2f costWithRI=%.2f (raw, over look-back) -> "
            "monthly netSavings=%.2f (x%.4f)",
            rtype, sku, region, term, days,
            props.get("recommendedQuantity") or props.get("recommendedQuantityNormalized") or 0,
            props.get("meterId"), raw_savings, raw_ondemand, raw_reserved, savings, factor,
        )
        if savings <= 0:
            logger.info("RI raw item DROPPED: resourceType=%r sku=%r reason=non_positive_savings (%.2f)",
                        raw_rtype, sku, savings)
            continue
        key = (rtype, str(sku), str(region), str(scope))
        group = groups.setdefault(key, {
            "resource_type": rtype,
            "category": category,
            "product": product,
            "sku": sku,
            "region": region,
            "scope": scope,
            "flexibility_group": props.get("instanceFlexibilityGroup"),
            "subscription_id": props.get("subscriptionId") or subscription_id,
            "terms": {},
            "_pref": {},
        })
        # One entry per term: keep the most-preferred look-back window (30 > 60 > 7).
        pref = _LOOKBACK_PREFERENCE.get(days, 0)
        if term in group["terms"] and pref <= group["_pref"].get(term, -1):
            continue
        group["_pref"][term] = pref
        group["terms"][term] = {
            "monthly_savings": savings,
            "monthly_ondemand": round(_amount(props.get("costWithNoReservedInstances")) * factor, 2),
            "monthly_reserved": round(_amount(props.get("totalCostWithReservedInstances")) * factor, 2),
            "quantity": props.get("recommendedQuantity") or props.get("recommendedQuantityNormalized") or 0,
        }
    for group in groups.values():
        group.pop("_pref", None)
    return list(groups.values())


# ── Reconciliation: historical recommendation → CURRENT subscription inventory ──────
#
# Azure's reservationRecommendations are SKU/usage-based and are computed from HISTORICAL usage; they
# do NOT return current resource IDs and they lag real inventory changes (a VM moved to another
# subscription, or deleted, can still appear in recommendations for days). A recommendation is
# therefore evidence of *historical* usage — it can NOT by itself prove the resource still exists in
# the assessed subscription. Before a VM recommendation becomes an actionable, client-facing finding we
# reconcile it against the CURRENT Resource-Graph VM inventory (which is scoped to the assessed
# subscriptions): only SKUs still present in the same subscription survive, and the affected resources
# are resolved from that current inventory — never fabricated from the historical recommendation.

_SIZE_DIGITS = re.compile(r"(?<=[a-z])\d+")


def _norm(s: Any) -> str:
    return str(s or "").strip().lower()


def _size_family(size: Any) -> str:
    """Collapse the instance-size digits so sizes in the same flexibility family share a signature:
    ``Standard_D2s_v5`` and ``Standard_D4s_v5`` both → ``standard_d#s_v5``. This mirrors Azure
    instance-size flexibility (a reservation bought for one size in a family applies to the others), so
    a present VM of a *different* size in the recommended family is not wrongly treated as absent."""
    return _SIZE_DIGITS.sub("#", _norm(size), count=1)


def _vm_matches_sku(vm_size: Any, rec_sku: Any) -> bool:
    a, b = _norm(vm_size), _norm(rec_sku)
    if not a or not b:
        return False
    return a == b or _size_family(a) == _size_family(b)


def reconcile_vm_recommendations(
    groups: List[Dict], current_vms: List[Dict], *, assessment_id: Any = None,
) -> List[Dict]:
    """Keep a VM reservation recommendation ONLY when the assessed subscription's CURRENT inventory
    still contains a matching VM; drop stale ones and attach the real current VMs as affected resources.

    `current_vms` is the live Resource-Graph VM inventory (running + deallocated), each row carrying
    ``id``, ``name``, ``subscriptionId``, ``location`` and ``vmSize`` — already scoped to the assessed
    subscriptions. A ``ri_vm`` group is matched **within its own subscription** (by SKU family, so
    instance-size flexibility isn't mistaken for absence). Groups with no current match are stale and
    excluded from actionable savings; matched groups gain ``current_vms`` (real resource ids) so the
    finding's affected-resource list/count comes from CURRENT inventory, not historical usage. Non-VM
    groups pass through unchanged (no comparable inventory is collected here) — documented, not silent.
    """
    by_sub: Dict[str, List[Dict]] = {}
    for vm in current_vms or []:
        sid = _norm(vm.get("subscriptionId") or vm.get("subscription_id"))
        if sid:
            by_sub.setdefault(sid, []).append(vm)

    kept: List[Dict] = []
    for g in groups:
        if g.get("category") != "ri_vm":
            kept.append(g)  # only VMs have a current-inventory counterpart to reconcile against
            continue
        sid = _norm(g.get("subscription_id"))
        rec_sku = g.get("sku") or ""
        terms = g.get("terms", {}) or {}
        qty = ((terms.get("P3Y") or terms.get("P1Y") or {}).get("quantity"))
        current_vm_count = len(by_sub.get(sid, []))
        matches = [vm for vm in by_sub.get(sid, []) if _vm_matches_sku(vm.get("vmSize"), rec_sku)]
        stale = not matches
        reason = (
            "NO_CURRENT_VM_IN_ASSESSED_SUBSCRIPTION" if current_vm_count == 0
            else "NO_CURRENT_VM_MATCHES_RECOMMENDED_SKU" if stale
            else "MATCHED_CURRENT_VM"
        )
        logger.info(
            "RI RECONCILIATION assessment=%s subscription=%s scope=%s sku=%s region=%s term=%s "
            "recommendedQuantity=%s currentVmCount=%d matchingVmCount=%d currentMatchingVMIds=%s "
            "staleRecommendation=%s finalAction=%s reason=%s",
            assessment_id, g.get("subscription_id"), g.get("scope"), rec_sku, g.get("region"),
            "/".join(sorted(terms.keys())) or "?", qty, current_vm_count, len(matches),
            [vm.get("id") for vm in matches][:10], stale, "EXCLUDED" if stale else "KEPT", reason,
        )
        if stale:
            continue
        g = dict(g)
        g["current_resources"] = [
            {"id": vm.get("id"), "name": vm.get("name"), "sku": vm.get("vmSize"),
             "region": vm.get("location"), "subscription_id": vm.get("subscriptionId")}
            for vm in matches
        ]
        kept.append(g)
    return kept


# ── SQL Database reservation reconciliation (vCore eligibility, current inventory) ──
#
# The same principle as VMs — CURRENT inventory outranks a historical recommendation — but the
# eligibility rule is different: Azure SQL Database reservation pricing applies ONLY to the vCore
# purchasing model. DTU databases (Basic/Standard/Premium tiers) are NOT reservation-eligible, so a
# recommendation is actionable only when the assessed subscription currently has a vCore SQL Database.
# We resolve affected resources from that current inventory — never from the historical recommendation,
# and never by manufacturing a database id from the subscription id.

_SQL_RESERVATION_CATEGORIES = {"sql_db_reserved_capacity"}
_VCORE_TIERS = {"generalpurpose", "businesscritical", "hyperscale"}
_DTU_TIERS = {"basic", "standard", "premium"}


def sql_purchasing_model(db: Dict) -> str:
    """'vCore' (reservation-eligible), 'DTU' (not eligible), or 'unknown'.

    Primary signal is `sku.tier` (GeneralPurpose/BusinessCritical/Hyperscale = vCore; Basic/Standard/
    Premium = DTU). When the tier is missing/blank in the inventory row we fall back to the SKU name so a
    vCore DB isn't wrongly treated as ineligible: GP_/BC_/HS_ prefixes or a Gen compute family = vCore;
    the DTU editions Basic/Standard/Premium or S#/P# sizes = DTU.
    """
    tier = _norm(db.get("tier"))
    if tier in _VCORE_TIERS:
        return "vCore"
    if tier in _DTU_TIERS:
        return "DTU"
    name = _norm(db.get("skuName") or db.get("sku"))
    if name.startswith(("gp_", "bc_", "hs_")) or any(g in name for g in ("gen4", "gen5", "gen8")):
        return "vCore"
    if name in _DTU_TIERS or re.match(r"^(basic|standard|premium|s\d+|p\d+)$", name):
        return "DTU"
    return "unknown"


def reconcile_sql_recommendations(
    groups: List[Dict], current_sql_dbs: List[Dict], *, assessment_id: Any = None,
) -> List[Dict]:
    """Keep a SQL Database reservation recommendation ONLY when the assessed subscription CURRENTLY has a
    reservation-eligible (vCore) SQL Database; drop stale/ineligible ones and attach the real current
    databases as affected resources.

    `current_sql_dbs` is the live Resource-Graph SQL Database inventory (id, name, subscriptionId,
    location, tier, skuName), scoped to the assessed subscriptions. Matching is by subscription + region
    (region is lenient when absent) among **vCore** databases only — DTU databases can't be reserved, so
    a subscription with only DTU (or zero) SQL databases yields no actionable SQL reservation finding.
    Non-SQL-Database groups pass through unchanged (documented; VMs are handled by their own reconciler).
    """
    total_sql = len(current_sql_dbs or [])
    by_sub: Dict[str, List[Dict]] = {}
    eligible = 0
    for db in current_sql_dbs or []:
        sid = _norm(db.get("subscriptionId") or db.get("subscription_id"))
        if sid and sql_purchasing_model(db) == "vCore":
            by_sub.setdefault(sid, []).append(db)
            eligible += 1

    sql_rec_count = sum(1 for g in groups if g.get("category") in _SQL_RESERVATION_CATEGORIES)
    kept: List[Dict] = []
    kept_sql = 0
    for g in groups:
        if g.get("category") not in _SQL_RESERVATION_CATEGORIES:
            kept.append(g)
            continue
        sid = _norm(g.get("subscription_id"))
        region = _norm(g.get("region"))
        terms = g.get("terms", {}) or {}
        qty = ((terms.get("P3Y") or terms.get("P1Y") or {}).get("quantity"))
        all_eligible = by_sub.get(sid, [])
        region_matches = ([db for db in all_eligible if _norm(db.get("location")) == region]
                          if region else list(all_eligible))
        # Region is a soft signal: a reservation is region-scoped, but Azure's recommendation region and
        # the ARG `location` string can diverge (canonical vs display name). If no DB matched the region
        # but the subscription DOES have eligible vCore SQL DBs, surface the finding against them rather
        # than false-excluding — and record that region was relaxed. This avoids the "SQL RI never
        # appears" failure while keeping the evidence.
        region_relaxed = bool(region and not region_matches and all_eligible)
        matches = all_eligible if region_relaxed else region_matches
        stale = not matches
        reason = (
            "NO_ELIGIBLE_VCORE_SQL_IN_SUBSCRIPTION" if not all_eligible
            else "MATCHED_CURRENT_SQL_DB" if not region_relaxed
            else "MATCHED_CURRENT_SQL_DB_REGION_RELAXED"
        )
        logger.info(
            "SQL RESERVATION RECONCILIATION assessment=%s subscription=%s scope=%s sku=%s recRegion=%s "
            "term=%s recommendedQuantity=%s currentEligibleVCoreDbs=%d matchingSqlCount=%d "
            "matchingSqlIds=%s staleRecommendation=%s finalAction=%s reason=%s",
            assessment_id, g.get("subscription_id"), g.get("scope"), g.get("sku"), g.get("region"),
            "/".join(sorted(terms.keys())) or "?", qty, len(all_eligible), len(matches),
            [db.get("id") for db in matches][:10], stale,
            "EXCLUDED" if stale else "INCLUDED", reason,
        )
        if stale:
            continue
        kept_sql += 1
        g = dict(g)
        g["current_resources"] = [
            {"id": db.get("id"), "name": db.get("name"), "sku": db.get("skuName"),
             "region": db.get("location"), "subscription_id": db.get("subscriptionId")}
            for db in matches
        ]
        kept.append(g)
    logger.info(
        "SQL RI SUMMARY assessment=%s currentSqlResourceCount=%d eligibleVCoreDbs=%d "
        "azureSqlRiRecommendations=%d keptSqlRecommendations=%d",
        assessment_id, total_sql, eligible, sql_rec_count, kept_sql,
    )
    return kept
