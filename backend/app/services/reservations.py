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

from typing import Any, Dict, List, Optional

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


def _sku_of(props: Dict) -> Optional[str]:
    """Prefer normalizedSize; fall back to a skuProperties name/value pair, then flexibility group."""
    sku = props.get("normalizedSize")
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
        sku = _sku_of(props)
        rtype = _resource_type(props, sku)
        meta = _RT_META.get(rtype)
        if meta is None or not sku:
            continue
        days = _lookback_days(props.get("lookBackPeriod")) or 30
        factor = 30.0 / days  # normalise the window's total to a monthly run-rate
        savings = round(_amount(props.get("netSavings")) * factor, 2)
        if savings <= 0:
            continue

        category, product = meta
        region = it.get("location") or props.get("location") or ""
        term = props.get("term") or "P1Y"
        scope = props.get("scope") or "Single"
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
