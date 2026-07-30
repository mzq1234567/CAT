"""
Azure Cost Management integration + Advisor cross-validation (Step 4).

Adds *actual* historical cost as a second data source. We pull actual spend per resource for a
trailing window, normalise it to a monthly figure, and cross-check Azure Advisor's *estimated*
savings against it. Any Advisor estimate that deviates from the actual cost trend beyond a
tolerance (default 10%) is flagged `needs_review` so a human can sanity-check it before it lands
in a customer report.

Design:
  * `build_cost_query()` / `parse_cost_rows()` are pure and unit-tested.
  * `get_actual_cost_by_resource()` does the (mockable) HTTP call via AzureClient.
  * `validate_savings()` is the pure cross-validation rule.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .azure_client import AzureClient

DEFAULT_WINDOW_DAYS = 30
DEFAULT_TOLERANCE = 0.10  # 10%

# Validation statuses
VALIDATED = "validated"
NEEDS_REVIEW = "needs_review"
UNVALIDATED = "unvalidated"


@dataclass
class ValidationResult:
    status: str  # VALIDATED | NEEDS_REVIEW | UNVALIDATED
    actual_monthly_cost: Optional[float]
    variance_pct: Optional[float]  # (estimate - actual) / actual * 100
    note: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "actual_monthly_cost": self.actual_monthly_cost,
            "variance_pct": self.variance_pct,
            "note": self.note,
        }


def _month_bounds(now: datetime):
    """Return (last_month_start, last_month_end, current_month_start) as aware datetimes."""
    first_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_month_end = first_this - timedelta(seconds=1)                 # 23:59:59 on last day of prev month
    last_month_start = (first_this - timedelta(days=1)).replace(day=1)  # 1st of prev month
    return last_month_start, last_month_end, first_this


def build_cost_query(now: Optional[datetime] = None, month_to_date: bool = False) -> Dict[str, Any]:
    """ActualCost grouped by ResourceId, for the LAST COMPLETE calendar month.

    A manual cost assessment reads *last month's bill* — never the current month, which isn't fully
    billed yet (Azure posts costs with a delay). We express it as an explicit `Custom` date range
    (more reliable across API versions than the named `TheLastMonth`). `month_to_date=True` switches
    to the current month so far — the fallback used only when the subscription is too new to have a
    complete previous month (otherwise it would report no cost at all).
    """
    now = now or datetime.now(timezone.utc)
    lm_start, lm_end, first_this = _month_bounds(now)
    start, end = (first_this, now) if month_to_date else (lm_start, lm_end)
    return {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {
            "from": start.strftime("%Y-%m-%dT00:00:00Z"),
            "to": end.strftime("%Y-%m-%dT23:59:59Z"),
        },
        "dataset": {
            "granularity": "None",
            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
            "grouping": [{"type": "Dimension", "name": "ResourceId"}],
        },
    }


def parse_cost_rows(payload: Dict[str, Any], days: int = DEFAULT_WINDOW_DAYS) -> Dict[str, float]:
    """Turn a Cost Management (last-month) response into `{resource_id_lower: monthly_cost}`.

    Column order is not assumed — indices resolve by name. The figure is already a full calendar
    month, so it is summed per resource id with no normalisation. (`days` is accepted for backward
    compatibility and ignored.)
    """
    props = payload.get("properties", {})
    columns = [c.get("name") for c in props.get("columns", [])]
    rows = props.get("rows", [])

    cost_idx = columns.index("Cost") if "Cost" in columns else 0
    rid_idx = columns.index("ResourceId") if "ResourceId" in columns else 1

    out: Dict[str, float] = {}
    for row in rows:
        if rid_idx >= len(row) or cost_idx >= len(row):
            continue
        rid = str(row[rid_idx] or "").lower()
        if not rid:
            continue
        try:
            cost = float(row[cost_idx] or 0)
        except (TypeError, ValueError):
            continue
        out[rid] = out.get(rid, 0.0) + cost
    return out


async def get_actual_cost_by_resource(
    client: AzureClient, subscription_id: str, days: int = DEFAULT_WINDOW_DAYS,
    now: Optional[datetime] = None,
) -> Dict[str, float]:
    """Fetch last-month cost per resource id, falling back to month-to-date for new subscriptions."""
    payload = await client.query_cost_management(subscription_id, build_cost_query(now=now))
    out = parse_cost_rows(payload)
    if not out:  # no complete previous month (brand-new subscription) → use the current month so far
        payload = await client.query_cost_management(
            subscription_id, build_cost_query(now=now, month_to_date=True))
        out = parse_cost_rows(payload)
    return out


# ── Total spend + per-service / per-area breakdown ──────────────────────────────

# Keyword → executive area. First match wins (order matters: databases before storage,
# since "sql database" also contains no storage keyword but be safe).
_SERVICE_AREA_KEYWORDS = [
    ("Databases", ["sql", "cosmos", "database", "mysql", "postgres", "mariadb", "redis"]),
    ("Storage", ["storage", "backup", "disk", "blob", "files", "data lake"]),
    ("Network", ["network", "bandwidth", "gateway", "expressroute", "load balancer",
                 "ip address", "dns", "front door", "cdn", "firewall", "traffic manager"]),
    ("Compute", ["virtual machine", "app service", "function", "kubernetes", "container",
                 "compute", "batch", "service fabric", "virtual desktop"]),
]


def area_for_service(service_name: str) -> str:
    """Best-effort grouping of an Azure ServiceName into an executive area."""
    s = (service_name or "").lower()
    for area, keywords in _SERVICE_AREA_KEYWORDS:
        if any(k in s for k in keywords):
            return area
    return "Other"


def build_service_cost_query(now: Optional[datetime] = None, month_to_date: bool = False) -> Dict[str, Any]:
    """ActualCost grouped by ServiceName for the last complete month — the whole-bill view."""
    body = build_cost_query(now=now, month_to_date=month_to_date)
    body["dataset"]["grouping"] = [{"type": "Dimension", "name": "ServiceName"}]
    return body


def parse_service_cost_rows(payload: Dict[str, Any], days: int = DEFAULT_WINDOW_DAYS) -> Dict[str, float]:
    """Turn a service-grouped (last-month) Cost Management response into `{service_name: monthly_cost}`."""
    props = payload.get("properties", {})
    columns = [c.get("name") for c in props.get("columns", [])]
    rows = props.get("rows", [])

    cost_idx = columns.index("Cost") if "Cost" in columns else 0
    svc_idx = columns.index("ServiceName") if "ServiceName" in columns else 1
    factor = 1.0  # already a full calendar month

    out: Dict[str, float] = {}
    for row in rows:
        if svc_idx >= len(row) or cost_idx >= len(row):
            continue
        service = str(row[svc_idx] or "Other")
        try:
            cost = float(row[cost_idx] or 0) * factor
        except (TypeError, ValueError):
            continue
        out[service] = out.get(service, 0.0) + cost
    return out


def extract_currency(payload: Dict[str, Any]) -> Optional[str]:
    """Read the billing currency from a Cost Management response's `Currency` column."""
    props = payload.get("properties", {})
    columns = [c.get("name") for c in props.get("columns", [])]
    if "Currency" not in columns:
        return None
    idx = columns.index("Currency")
    for row in props.get("rows", []):
        if idx < len(row) and row[idx]:
            return str(row[idx]).upper()
    return None


def spend_by_area(service_costs: Dict[str, float]) -> Dict[str, float]:
    """Aggregate {service: cost} into {area: cost}."""
    out: Dict[str, float] = {}
    for service, cost in service_costs.items():
        area = area_for_service(service)
        out[area] = round(out.get(area, 0.0) + cost, 2)
    return out


async def get_actual_cost_by_service(
    client: AzureClient, subscription_id: str, days: int = DEFAULT_WINDOW_DAYS,
    now: Optional[datetime] = None,
) -> Dict[str, float]:
    """Fetch last-month cost per Azure service, falling back to month-to-date for new subscriptions."""
    costs, _ = await get_service_costs_and_currency(client, subscription_id, now=now)
    return costs


async def get_service_costs_and_currency(
    client: AzureClient, subscription_id: str, days: int = DEFAULT_WINDOW_DAYS,
    now: Optional[datetime] = None,
) -> tuple[Dict[str, float], Optional[str]]:
    """Per-service last-month cost + billing currency; falls back to month-to-date for new subs."""
    payload = await client.query_cost_management(subscription_id, build_service_cost_query(now=now))
    costs = parse_service_cost_rows(payload)
    if not costs:  # brand-new subscription with no complete previous month → current month so far
        payload = await client.query_cost_management(
            subscription_id, build_service_cost_query(now=now, month_to_date=True))
        costs = parse_service_cost_rows(payload)
    return costs, extract_currency(payload)


# ── Month-over-month consistency (validation) ───────────────────────────────────

def build_monthly_history_query(months: int = 4, now: Optional[datetime] = None) -> Dict[str, Any]:
    """ActualCost per resource, per MONTH, over the last `months` complete calendar months.

    Mirrors the manual check: is a VM costing the same each month (steadily used → safe to reserve),
    or does it swing (idle some months, busy others → don't over-commit)?
    """
    now = now or datetime.now(timezone.utc)
    first_of_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = first_of_this - timedelta(seconds=1)          # last moment of the last complete month
    start = first_of_this
    for _ in range(months):
        start = (start - timedelta(days=1)).replace(day=1)  # step back `months` month-starts
    return {
        "type": "ActualCost", "timeframe": "Custom",
        "timePeriod": {"from": start.strftime("%Y-%m-%dT00:00:00Z"),
                       "to": end.strftime("%Y-%m-%dT23:59:59Z")},
        "dataset": {"granularity": "Monthly",
                    "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
                    "grouping": [{"type": "Dimension", "name": "ResourceId"}]},
    }


def parse_monthly_history(payload: Dict[str, Any]) -> Dict[str, list]:
    """Monthly-granularity response → `{resource_id_lower: [cost_per_month, ...]}`, oldest → newest.

    Rows carry a month column (BillingMonth / UsageDate); we sort by it so the LAST element is the
    most recent complete month — which the cost basis reads directly.
    """
    props = payload.get("properties", {})
    columns = [c.get("name") for c in props.get("columns", [])]
    cost_idx = columns.index("Cost") if "Cost" in columns else 0
    rid_idx = columns.index("ResourceId") if "ResourceId" in columns else 1
    month_idx = next((columns.index(n) for n in ("BillingMonth", "UsageDate", "BillingPeriod")
                      if n in columns),
                     next((i for i, n in enumerate(columns)
                           if n not in ("Cost", "ResourceId", "Currency")), None))

    pairs: Dict[str, list] = {}
    for row in props.get("rows", []):
        if rid_idx >= len(row) or cost_idx >= len(row):
            continue
        rid = str(row[rid_idx] or "").lower()
        if not rid:
            continue
        try:
            cost = float(row[cost_idx] or 0)
        except (TypeError, ValueError):
            continue
        month = str(row[month_idx]) if (month_idx is not None and month_idx < len(row)) else ""
        pairs.setdefault(rid, []).append((month, cost))
    return {rid: [c for _, c in sorted(ps, key=lambda p: p[0])] for rid, ps in pairs.items()}


async def get_cost_map_and_consistency(
    client: AzureClient, subscription_id: str, months: int = 4, now: Optional[datetime] = None,
) -> tuple[Dict[str, float], Dict[str, Dict]]:
    """One monthly-history query → BOTH the last-month cost basis AND month-over-month consistency.

    Reading both from a single Cost Management call (instead of two) halves the load on the heavily
    throttled billing API. `cost_map` = each resource's most recent complete month; if that's empty
    (brand-new subscription) it falls back to month-to-date so cost data is never lost.
    """
    payload = await client.query_cost_management(
        subscription_id, build_monthly_history_query(months=months, now=now))
    history = parse_monthly_history(payload)
    consistency = cost_consistency(history)
    cost_map = {rid: costs[-1] for rid, costs in history.items() if costs and costs[-1] > 0}
    if not cost_map:  # new subscription with no complete month → current month so far
        mtd = await client.query_cost_management(
            subscription_id, build_cost_query(now=now, month_to_date=True))
        cost_map = parse_cost_rows(mtd)
    return cost_map, consistency


def cost_consistency(history: Dict[str, list], min_stable_months: int = 2,
                     cv_threshold: float = 0.25) -> Dict[str, Dict]:
    """Per-resource steadiness: mean monthly cost, months billed, and a `stable` flag.

    `stable` = billed in ≥ `min_stable_months` months with a coefficient of variation ≤ threshold
    (costs within ~25% of each other) — i.e. the resource is consistently used, so a reservation is
    safe. An erratic resource (big month-to-month swings) is flagged so commitments carry a caveat.
    """
    out: Dict[str, Dict] = {}
    for rid, costs in history.items():
        billed = [c for c in costs if c > 0]
        if not billed:
            out[rid] = {"months": len(costs), "billed_months": 0, "mean": 0.0, "stable": False}
            continue
        mean = sum(billed) / len(billed)
        cv = (sum((c - mean) ** 2 for c in billed) / len(billed)) ** 0.5 / mean if mean else 0.0
        out[rid] = {
            "months": len(costs), "billed_months": len(billed), "mean": round(mean, 2),
            "cv": round(cv, 2), "stable": len(billed) >= min_stable_months and cv <= cv_threshold,
        }
    return out


async def get_cost_consistency(
    client: AzureClient, subscription_id: str, months: int = 4, now: Optional[datetime] = None,
) -> Dict[str, Dict]:
    """Fetch per-resource month-over-month consistency for a subscription. Empty on no access."""
    body = build_monthly_history_query(months=months, now=now)
    payload = await client.query_cost_management(subscription_id, body)
    return cost_consistency(parse_monthly_history(payload))


def validate_savings(
    estimate_monthly: float,
    resource_id: Optional[str],
    cost_map: Dict[str, float],
    tolerance: float = DEFAULT_TOLERANCE,
) -> ValidationResult:
    """Cross-check an estimated monthly saving against actual resource cost.

    Rule: a saving cannot plausibly exceed what the resource actually costs. If the estimate
    exceeds actual monthly cost by more than `tolerance`, flag `needs_review`. If there is no
    actual-cost data for the resource, the estimate is `unvalidated` (not wrong, just unproven).
    """
    if not resource_id:
        return ValidationResult(UNVALIDATED, None, None, "No resource ID to match against actual cost.")

    actual = cost_map.get(resource_id.lower())
    if actual is None:
        return ValidationResult(UNVALIDATED, None, None, "No actual cost data for this resource in the window.")

    if actual <= 0:
        if estimate_monthly > 0:
            return ValidationResult(
                NEEDS_REVIEW, round(actual, 2), None,
                "Advisor estimates savings but the resource shows ~$0 actual cost.",
            )
        return ValidationResult(VALIDATED, round(actual, 2), 0.0, "No cost and no estimated savings.")

    variance_pct = round((estimate_monthly - actual) / actual * 100.0, 1)
    if estimate_monthly > actual * (1 + tolerance):
        return ValidationResult(
            NEEDS_REVIEW, round(actual, 2), variance_pct,
            (f"Estimated saving ${estimate_monthly:.2f}/mo exceeds actual resource cost "
             f"${actual:.2f}/mo by >{int(tolerance * 100)}% (variance {variance_pct:+.1f}%)."),
        )
    return ValidationResult(
        VALIDATED, round(actual, 2), variance_pct,
        (f"Estimated saving ${estimate_monthly:.2f}/mo is within the actual cost trend "
         f"${actual:.2f}/mo (variance {variance_pct:+.1f}%)."),
    )
