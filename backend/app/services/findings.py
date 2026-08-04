"""
Findings engine (Step 6).

Detects cost-optimisation findings across four families:
  * unattached / orphaned  — ARG state is authoritative (unattached disk, idle App Service Plan…)
  * idle                   — running VM with avg CPU below the idle threshold
  * oversized              — running VM with low-but-not-idle CPU (downsize candidate)
  * reserved-instance      — steadily-running VM cheaper on a 1yr reservation than pay-as-you-go
Plus Azure Advisor's own cost recommendations, re-scored consistently.

Every finding carries:
  * severity   — critical / high / medium / low (savings magnitude, Advisor impact can raise it)
  * confidence — 0..1 from data freshness/volume, pricing source, and validation outcome
  * advisor_recommendation_id — correlated Advisor rec id when one matches the resource
  * validation — cross-check vs actual Cost Management spend (Step 4)
  * debug_reason — DEV-ONLY plain-language trigger explanation, gated by DEBUG_FINDINGS_REASONING

Live pricing (Step 2) and actual-cost validation (Step 4) are injected, so the engine is fully
unit-testable without any network.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from .cost_management import UNVALIDATED, VALIDATED, ValidationResult, validate_savings
from .currency import from_usd, symbol, to_usd
from .estimates import (
    GRS_VAULT_MONTHLY_USD,
    SNAPSHOT_PER_GB_MONTHLY_USD,
    SQL_AHB_LICENCE_PER_VCORE_MONTHLY_USD,
)
from .pricing import PricingEngine, PricingUnavailableError
from .vm_specs import VmSpec, get_spec, smaller_same_series

logger = logging.getLogger("cat.findings")

# ── Thresholds — decided on PEAK usage over 30 days, CPU *and* memory together ────
# Rationale: a shutdown/downsize decision must be safe at the busiest moment (peak, not average —
# averaging hides scheduled/batch spikes), and safe on BOTH resources — a VM can be CPU-idle while
# doing real work in memory (a cache, a large heap), so CPU alone is not sufficient evidence.
IDLE_MAX_CPU = 5.0            # peak CPU below this...
IDLE_MAX_MEMORY_PCT = 10.0    # ...AND peak memory used below this → does effectively nothing.
DOWNSIZE_HEADROOM_CEILING = 70.0  # a candidate (smaller) SKU must keep BOTH projected peaks ≤ this.
METRIC_WINDOW_DAYS = 30


def _median(values: List[float]) -> Optional[float]:
    """Median of the positive values (robust central estimate), or None if there are none."""
    vals = sorted(v for v in values if v is not None and v > 0)
    if not vals:
        return None
    m = len(vals) // 2
    return vals[m] if len(vals) % 2 else round((vals[m - 1] + vals[m]) / 2, 4)


def _vcpus(sku: str) -> Optional[int]:
    """vCPU count for a VM SKU — from the curated spec table, else parsed from the SKU name.

    Azure encodes the vCPU count as the first number in the size (Standard_D4s_v3 → 4,
    Standard_B2ms → 2, Standard_DC1ds_v3 → 1), so a simple first-digit parse is a reliable fallback
    for sizes the curated table doesn't carry.
    """
    spec = get_spec(sku)
    if spec and spec.vcpu:
        return spec.vcpu
    m = re.search(r"\d+", sku or "")
    return int(m.group()) if m else None


# App Service Plan specs: sku → (series, cores, memory_gb). Downsizing stays within a series (same
# feature set), stepping to a smaller instance. Source: Azure App Service pricing (per-instance sizes).
_ASP_SPECS: Dict[str, tuple] = {
    "b1": ("B", 1, 1.75), "b2": ("B", 2, 3.5), "b3": ("B", 4, 7.0),
    "s1": ("S", 1, 1.75), "s2": ("S", 2, 3.5), "s3": ("S", 4, 7.0),
    "p1v2": ("Pv2", 1, 3.5), "p2v2": ("Pv2", 2, 7.0), "p3v2": ("Pv2", 4, 14.0),
    "p0v3": ("Pv3", 1, 4.0), "p1v3": ("Pv3", 2, 8.0), "p2v3": ("Pv3", 4, 16.0), "p3v3": ("Pv3", 8, 32.0),
    "i1v2": ("Iv2", 2, 8.0), "i2v2": ("Iv2", 4, 16.0), "i3v2": ("Iv2", 8, 32.0),
}


def find_asp_downsize_target(
    sku: str, peak_cpu: Optional[float], peak_mem: Optional[float],
    ceiling: float = DOWNSIZE_HEADROOM_CEILING,
):
    """Smallest same-series ASP SKU where BOTH projected CPU and memory stay under `ceiling`.

    Projecting onto a smaller instance scales utilisation up by the capacity ratio (halving cores
    roughly doubles CPU %), so a downsize is only safe when the busiest-moment peaks still clear the
    ceiling on the target. Returns `(sku, cores, memory_gb)` or None (already smallest / no headroom).
    When memory couldn't be measured, CPU alone is used (the caller lowers confidence for that case).
    """
    cur = _ASP_SPECS.get((sku or "").lower())
    if cur is None or peak_cpu is None:
        return None
    series, cur_cores, cur_mem = cur
    smaller = sorted(
        ((name, cores, mem) for name, (ser, cores, mem) in _ASP_SPECS.items()
         if ser == series and cores < cur_cores),
        key=lambda x: x[1],  # ascending cores → smallest (cheapest) first
    )
    for name, cores, mem in smaller:
        proj_cpu = peak_cpu * (cur_cores / cores)
        proj_mem = peak_mem * (cur_mem / mem) if peak_mem is not None else 0.0
        if proj_cpu <= ceiling and proj_mem <= ceiling:
            return name, cores, mem
    return None


# vCore options. Single Databases (GP/BC Gen5) step in small increments; Managed Instances step in
# larger ones. Downsizing moves to fewer vCores within the tier.
_SQL_VCORE_LADDER = [2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 24, 32, 40, 80]
_SQL_MI_VCORE_LADDER = [4, 8, 16, 24, 32, 40, 64, 80]

# Standard SSD provides ~500 IOPS / ~60 MB/s baseline on every tier (larger tiers more). A Premium disk
# whose PEAK stays well under that — the 70% headroom ceiling — runs comfortably on Standard SSD.
_STANDARD_SSD_BASELINE_IOPS = 500
_STANDARD_SSD_BASELINE_MBPS = 60

# Environment classification for Reserved Instance targeting. A Reserved Instance suits a long-lived
# workload — which a provisioned production VM almost always is — so we recommend an RI for prod VMs and
# SKIP dev/test/staging (short-lived, not worth a 1–3 year commitment). Tags are the reliable signal; the
# VM/RG name is a best-effort fallback. Unclassifiable → assumed production (but flagged for the reviewer).
_PROD_ENV_KEYWORDS = ("prod", "prd", "production", "live")
_NONPROD_ENV_KEYWORDS = ("dev", "test", "qa", "uat", "stage", "staging", "stg", "sandbox", "sbx",
                         "demo", "poc", "lab", "scratch", "temp", "preprod", "nonprod")
_ENV_TAG_KEYS = ("environment", "env", "tier", "stage", "usage", "deployment")

# Typical Azure VM Reserved Instance discounts — used only when a SKU's retail RI price can't be fetched,
# so a production VM still gets a (clearly-estimated) recommendation instead of being silently dropped.
_RI_DEFAULT_DISCOUNT_1YR = 0.40
_RI_DEFAULT_DISCOUNT_3YR = 0.60


def _vm_environment(vm: Dict) -> Tuple[str, str]:
    """Classify a VM as ('prod' | 'nonprod' | 'unknown', reason) from tags first, then name/RG keywords."""
    tags = {str(k).lower(): str(v).lower() for k, v in (vm.get("tags") or {}).items()}
    for key in _ENV_TAG_KEYS:
        val = tags.get(key)
        if not val:
            continue
        if any(w in val for w in _NONPROD_ENV_KEYWORDS):
            return "nonprod", f"tag {key}={val}"
        if any(w in val for w in _PROD_ENV_KEYWORDS):
            return "prod", f"tag {key}={val}"
    # Name / resource-group fallback: tokenise so 'test' doesn't match 'latest', etc.
    tokens = set(re.split(r"[^a-z0-9]+", f"{vm.get('name', '')} {vm.get('resourceGroup', '')}".lower()))
    if tokens & set(_NONPROD_ENV_KEYWORDS):
        return "nonprod", "name/resource-group indicates non-production"
    if tokens & set(_PROD_ENV_KEYWORDS):
        return "prod", "name/resource-group indicates production"
    return "unknown", "no environment tag — assumed production"


def find_sql_vcore_target(
    current_vcores: int, peaks: List[Optional[float]],
    ladder: Optional[List[int]] = None, ceiling: float = DOWNSIZE_HEADROOM_CEILING,
) -> Optional[int]:
    """Smallest vCore count below `current_vcores` (from `ladder`) where EVERY measured utilisation
    peak, scaled up by the capacity ratio (halving vCores ~doubles %), still clears `ceiling`. `peaks`
    may hold None entries (metric unavailable) — those are ignored; at least one real peak is required.
    None → no safe smaller size."""
    ladder = ladder or _SQL_VCORE_LADDER
    usable = [p for p in peaks if p is not None]
    if not current_vcores or current_vcores <= ladder[0] or not usable:
        return None
    for target in ladder:
        if target >= current_vcores:
            break
        ratio = current_vcores / target
        if all(p * ratio <= ceiling for p in usable):
            return target
    return None

SEV_ORDER = ["low", "medium", "high", "critical"]

CATEGORY_DISPLAY = {
    "unattached_managed_disks": "Unattached Managed Disks",
    "orphaned_public_ips": "Orphaned Public IP Addresses",
    "idle_app_service_plans": "Idle App Service Plans",
    "app_service_plan_rightsizing": "App Service Plan Rightsizing",
    "sql_db_rightsizing": "SQL Database Rightsizing",
    "sql_mi_rightsizing": "SQL Managed Instance Rightsizing",
    "disk_rightsizing": "Disk SKU Rightsizing (Premium → Standard)",
    "deallocated_vms": "Deallocated Virtual Machines",
    "paused_sql_databases": "Paused/Inactive SQL Databases",
    "stopped_sql_managed_instances": "Stopped SQL Managed Instances",
    "idle_vms": "Idle Virtual Machines",
    "oversized_vms": "Oversized Virtual Machines",
    "ri_vm": "Reserved Instance (VM)",
    "vm_rightsizing": "VM Rightsizing",
    "windows_ahb": "Windows Azure Hybrid Benefit",
    "sql_ahb": "SQL Server Azure Hybrid Benefit",
    # Reserved-capacity purchase recs from Azure's own reservation engine (Consumption API).
    "sql_db_reserved_capacity": "SQL Reserved Capacity",
    "sql_mi_reserved_capacity": "SQL MI Reserved Capacity",
    "managed_disk_reserved_capacity": "Managed Disk Reserved Capacity",
    "mysql_reserved_capacity": "Database Reserved Capacity",
    "cosmos_reserved_capacity": "Cosmos DB Reserved Capacity",
    "app_service_reserved_capacity": "App Service Reserved Capacity",
    "azure_files_reserved_capacity": "Storage Reserved Capacity",
    "advisor_cost": "Azure Advisor Cost Recommendation",
    # Broader coverage — cost-bearing orphans/waste (new)
    "orphaned_snapshots": "Orphaned Disk Snapshots",
    "empty_load_balancers": "Empty Load Balancers",
    "idle_nat_gateways": "Idle NAT Gateways",
    "bastion_hosts": "Azure Bastion — Review",
    "backup_redundancy": "Backup Redundancy (GRS→LRS)",
}


# ── Rule-driven orphan/waste detection (DRY — one rule per resource type) ────────
# Load Balancer / NAT Gateway / Bastion are priced LIVE (see detect_orphans → PricingEngine); their
# lambda here is just the dated fallback. Snapshot (size × per-GB) and GRS vault stay estimates
# because their cost depends on data volume Resource Graph doesn't expose. All values are grounded by
# the actual-cost cap + validation when Cost Management data is available.


@dataclass(frozen=True)
class OrphanRule:
    category: str
    resource_type: str
    recommendation: str
    describe: Callable[[Dict], str]
    monthly_cost: Callable[[Dict], float]
    base_confidence: float = 0.9


def _short_date(row: Dict) -> str:
    return str(row.get("timeCreated") or "")[:10] or "unknown date"


ORPHAN_RULES: Dict[str, OrphanRule] = {
    "orphaned_snapshots": OrphanRule(
        "orphaned_snapshots", "microsoft.compute/snapshots",
        "Delete snapshots no longer needed for recovery or compliance.",
        lambda r: (f"Snapshot '{r.get('name')}' ({int(r.get('diskSizeGB') or 0)} GB, created "
                   f"{_short_date(r)}) is retained and accruing storage cost."),
        lambda r: round(int(r.get("diskSizeGB") or 0) * SNAPSHOT_PER_GB_MONTHLY_USD, 2),
        0.85,
    ),
    "empty_load_balancers": OrphanRule(
        "empty_load_balancers", "microsoft.network/loadbalancers",
        "Delete the load balancer if it is not routing traffic.",
        lambda r: (f"Standard Load Balancer '{r.get('name')}' has no backend pool — it bills "
                   "without balancing anything."),
        lambda r: 0.0, 0.85,  # priced live in detect_orphans; this lambda is unused for this bucket
    ),
    "idle_nat_gateways": OrphanRule(
        "idle_nat_gateways", "microsoft.network/natgateways",
        "Delete the NAT gateway if no subnet uses it.",
        lambda r: (f"NAT Gateway '{r.get('name')}' is not associated with any subnet, yet bills a "
                   "fixed hourly rate."),
        lambda r: 0.0, 0.85,  # priced live in detect_orphans
    ),
    "bastion_hosts": OrphanRule(
        "bastion_hosts", "microsoft.network/bastionhosts",
        "Confirm Bastion is still needed, or deallocate it when not in use.",
        lambda r: (f"Azure Bastion '{r.get('name')}' ({r.get('skuName') or 'Standard'}) is a "
                   "fixed-cost resource that bills whether or not it's used — verify it is still required."),
        lambda r: 0.0, 0.6,  # priced live in detect_orphans
    ),
    "geo_redundant_vaults": OrphanRule(
        "backup_redundancy", "microsoft.recoveryservices/vaults",
        "If cross-region recovery isn't required, switch backup storage from Geo- to Locally-"
        "redundant (LRS) to roughly halve backup storage cost.",
        lambda r: (f"Recovery Services vault '{r.get('name')}' uses geo-redundant (GRS) backup "
                   "storage. If a geo-secondary copy isn't required, LRS costs about half."),
        # Estimate — actual depends on backup volume (not available from Resource Graph).
        lambda r: GRS_VAULT_MONTHLY_USD, 0.4,
    ),
}

# Buckets whose flat rate is fetched live from the Retail Prices API (falls back to the dated estimate).
_LIVE_PRICED_ORPHANS = {"empty_load_balancers", "idle_nat_gateways", "bastion_hosts"}


# ── Scoring helpers (pure) ───────────────────────────────────────────────────────

def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def severity_from_savings(monthly: float, advisor_impact: str = "", currency: str = "USD") -> str:
    # Bands are defined in USD; convert the (billing-currency) saving so INR/GBP/etc. aren't all
    # flagged critical just because the number is larger/smaller in absolute terms.
    usd = to_usd(monthly, currency)
    base = (
        "critical" if usd >= 300
        else "high" if usd >= 100
        else "medium" if usd >= 20
        else "low"
    )
    if advisor_impact.lower() == "high" and SEV_ORDER.index("high") > SEV_ORDER.index(base):
        base = "high"
    return base


def metrics_confidence(datapoints: int, window_days: int = METRIC_WINDOW_DAYS) -> float:
    """Confidence from metric volume: more datapoints over the window → higher confidence."""
    if datapoints <= 0:
        return 0.3
    ratio = min(datapoints / max(window_days, 1), 1.0)
    return round(0.6 + 0.35 * ratio, 2)  # 0.6 .. 0.95


def combine_confidence(base: float, validation: Optional[ValidationResult], has_price: bool) -> float:
    """Fold pricing availability + validation outcome into a base confidence."""
    score = base
    if not has_price:
        score *= 0.7
    if validation is not None:
        if validation.status == "needs_review":
            score = min(score, 0.5)
        elif validation.status == "validated":
            score = min(1.0, score + 0.05)
    return round(clamp01(score), 2)


def find_downsize_target(
    current_sku: str, peak_cpu_pct: float, peak_memory_pct: Optional[float],
    ceiling: float = DOWNSIZE_HEADROOM_CEILING,
) -> Optional[VmSpec]:
    """Walk the same-series ladder and return the SMALLEST candidate SKU that keeps projected peak
    CPU and (if known) peak memory both ≤ `ceiling` on that candidate. None if no candidate fits,
    the current SKU is unknown, or it's already the smallest in its family.

    When `peak_memory_pct` is None (memory could not be measured), only CPU is checked — the
    caller is responsible for reflecting that reduced certainty in confidence/description; this
    function only answers "does a smaller SKU fit," not "how sure are we."
    """
    current = get_spec(current_sku)
    if current is None:
        return None
    cpu_cores_used = (peak_cpu_pct / 100.0) * current.vcpu
    memory_gb_used = (peak_memory_pct / 100.0) * current.memory_gb if peak_memory_pct is not None else None

    # Projected utilisation strictly increases as the candidate gets smaller (fixed absolute
    # usage / shrinking capacity), so once a candidate fails to fit, nothing smaller will either —
    # walk largest→smallest and stop at the first miss, keeping the last (smallest) fit.
    best: Optional[VmSpec] = None
    for candidate in smaller_same_series(current_sku):  # largest→smallest
        projected_cpu = (cpu_cores_used / candidate.vcpu) * 100.0
        if projected_cpu > ceiling:
            break
        if memory_gb_used is not None:
            projected_memory = (memory_gb_used / candidate.memory_gb) * 100.0
            if projected_memory > ceiling:
                break
        best = candidate
    return best


def build_advisor_index(recommendations: List[Dict]) -> Dict[str, Dict]:
    """Map resource_id (lower) → Advisor recommendation, for correlation."""
    index: Dict[str, Dict] = {}
    for rec in recommendations:
        rid = rec.get("properties", {}).get("resourceMetadata", {}).get("resourceId", "")
        if rid:
            index.setdefault(rid.lower(), rec)
    return index


# ── Advisor classification (pay-as-you-go recs Azure already computed) ───────────

def _extract_savings(ext: Dict) -> Tuple[float, float]:
    monthly = float(ext.get("savingsAmount") or ext.get("monthlySavingsAmount") or 0)
    annual = float(ext.get("annualSavingsAmount") or 0)
    if annual == 0 and monthly > 0:
        annual = monthly * 12
    if monthly == 0 and annual > 0:
        monthly = annual / 12
    return monthly, annual


def _parse_ids(resource_id: str) -> Tuple[str, str]:
    parts = resource_id.split("/")
    sub = parts[2] if len(parts) > 2 and parts[1].lower() == "subscriptions" else ""
    rg = parts[4] if len(parts) > 4 and parts[3].lower() == "resourcegroups" else ""
    return sub, rg


# ── The engine ────────────────────────────────────────────────────────────────────

class FindingsEngine:
    """Builds findings from ARG inventory, VM metrics, and Advisor recs.

    `pricing` and `cost_map` are injected so the engine needs no network in tests.
    `debug` gates the DEV-ONLY `debug_reason` field.
    """

    def __init__(
        self,
        pricing: PricingEngine,
        cost_map: Optional[Dict[str, float]] = None,
        advisor_index: Optional[Dict[str, Dict]] = None,
        snapshot_iso: str = "",
        debug: bool = False,
        reservation_basis: str = "combined",
        cost_consistency: Optional[Dict[str, Dict]] = None,
        currency: str = "USD",
    ):
        self._pricing = pricing
        self._cost_map = cost_map or {}
        self._advisor_index = advisor_index or {}
        self._consistency = cost_consistency or {}
        self._currency = (currency or "USD").upper()
        self._snapshot = snapshot_iso or "the inventory snapshot"
        self._debug = debug
        self._reservation_basis = reservation_basis

    # -- shared builder --------------------------------------------------------

    def _advisor_id_for(self, resource_id: Optional[str]) -> Optional[str]:
        if not resource_id:
            return None
        rec = self._advisor_index.get(resource_id.lower())
        return rec.get("id") if rec else None

    def _finding(
        self, category: str, resource: Dict, resource_type: str, monthly: float,
        base_confidence: float, description: str, recommendation: str,
        *, has_price: bool = True, advisor_impact: str = "",
        debug_reason: Optional[str] = None, extra_details: Optional[Dict] = None,
        grounded: bool = False, actual_cost_override: Optional[float] = None,
    ) -> Dict:
        monthly = round(monthly or 0.0, 2)
        resource_id = resource.get("id")
        # Validation compares the *original* estimate to actual cost (records the overage).
        validation = validate_savings(monthly, resource_id, self._cost_map) if monthly > 0 else None
        # When a finding's saving is grounded in a DIFFERENT resource's cost than the one it's attributed
        # to (e.g. a deallocated VM's saving is the cost of its still-billing DISKS, not the VM's ~0
        # compute), the caller supplies the right cost basis so the cap below doesn't clamp against the
        # wrong (near-zero) figure.
        if actual_cost_override is not None and monthly > 0:
            validation = ValidationResult(
                VALIDATED, round(actual_cost_override, 2), 0.0,
                "Grounded in the attached resources' actual billed cost.")

        # Cap FIRST: you can't save more on a resource than it actually costs. Clamp the estimate to the
        # real billed cost so totals never exceed spend — and, since the figure now IS the actual cost,
        # treat it as validated (not "needs review", which would flag a scary +4000% variance on a
        # number that's now exactly right).
        capped = False
        if validation is not None and validation.actual_monthly_cost is not None:
            actual = validation.actual_monthly_cost
            if actual >= 0 and monthly > actual:
                monthly = round(actual, 2)
                capped = True
                validation = ValidationResult(
                    VALIDATED, round(actual, 2), 0.0, "Capped to the resource's actual billed cost.")

        # A saving DERIVED from the resource's actual billed cost (AHB = licence fraction of real cost;
        # commitment = discount % of real cost) is inherently validated even when it's an aggregate
        # with no single resource id to match — otherwise it mislabels as an unvalidated list-price
        # estimate, which is exactly backwards for the numbers that ARE grounded.
        if grounded and monthly > 0 and (validation is None or validation.status == UNVALIDATED):
            validation = ValidationResult(VALIDATED, None, None, "Grounded in the resource's actual billed cost.")

        confidence = combine_confidence(base_confidence, validation, has_price)
        advisor_id = self._advisor_id_for(resource_id)
        # An Advisor rec corroborating our own detection raises confidence.
        if advisor_id and category not in ("advisor_cost", "ri_vm"):
            confidence = round(min(1.0, confidence + 0.05), 2)

        details = dict(extra_details or {})
        if validation is not None:
            details["validation_note"] = validation.note
        if capped:
            details["savings_capped_at_actual_cost"] = True

        return {
            "category": category,
            "display_name": CATEGORY_DISPLAY.get(category, category),
            "resource_id": resource_id,
            "resource_name": resource.get("name"),
            "subscription_id": resource.get("subscriptionId"),
            "resource_group": resource.get("resourceGroup"),
            "resource_type": resource_type,
            "estimated_savings_monthly": monthly,
            "estimated_savings_annual": round(monthly * 12, 2),
            "severity": severity_from_savings(monthly, advisor_impact, self._currency),
            "confidence": confidence,
            "description": description,
            "recommendation": recommendation,
            "advisor_recommendation_id": advisor_id,
            "validation_status": validation.status if validation else None,
            "validation_variance_pct": validation.variance_pct if validation else None,
            "actual_monthly_cost": validation.actual_monthly_cost if validation else None,
            # TODO: remove or gate behind admin-only role before prod (debug scaffolding).
            "debug_reason": debug_reason if self._debug else None,
            "details": details,
        }

    # -- unattached / orphaned (ARG-authoritative) -----------------------------

    async def detect_unattached_disks(self, disks: List[Dict]) -> List[Dict]:
        out = []
        for disk in disks:
            sku = disk.get("skuName") or "Standard_LRS"
            size = int(disk.get("diskSizeGB") or 0)
            region = disk.get("location") or "eastus"
            try:
                price = await self._pricing.get_managed_disk_monthly_price(region, sku, size)
            except PricingUnavailableError:
                price = None
            monthly = price or 0.0
            reason = (
                f"disk unattached: diskState=='Unattached' in Resource Graph as of {self._snapshot}; "
                f"{size} GB {sku} in {region} priced at ${monthly:.2f}/mo "
                f"({'live retail' if price is not None else 'no price available'})."
            )
            out.append(self._finding(
                "unattached_managed_disks", disk, "microsoft.compute/disks", monthly,
                base_confidence=0.9,
                description=(f"Managed disk '{disk.get('name')}' ({size} GB, {sku}) is unattached "
                            "and accruing storage cost with no VM using it."),
                recommendation="Delete the disk if unneeded, or re-attach it to a VM.",
                has_price=price is not None, debug_reason=reason, extra_details=disk,
            ))
        return out

    async def detect_orphaned_public_ips(self, ips: List[Dict]) -> List[Dict]:
        out = []
        for pip in ips:
            sku = pip.get("skuName") or "Basic"
            region = pip.get("location") or "eastus"
            try:
                price = await self._pricing.get_public_ip_monthly_price(region, sku)
            except PricingUnavailableError:
                price = None
            monthly = price or 0.0
            reason = (
                f"public IP orphaned: no ipConfiguration/natGateway in Resource Graph as of "
                f"{self._snapshot}; {sku} IP in {region} priced ${monthly:.2f}/mo."
            )
            out.append(self._finding(
                "orphaned_public_ips", pip, "microsoft.network/publicipaddresses", monthly,
                base_confidence=0.9,
                description=(f"Public IP '{pip.get('name')}' ({sku}) is not associated with any "
                            "resource and is billing idle."),
                recommendation="Delete the Public IP if it is no longer needed.",
                has_price=price is not None, debug_reason=reason, extra_details=pip,
            ))
        return out

    async def detect_idle_app_service_plans(self, plans: List[Dict]) -> List[Dict]:
        out = []
        for plan in plans:
            sku = plan.get("skuName") or ""
            region = plan.get("location") or "eastus"
            monthly = await self._pricing.get_app_service_plan_monthly_price(region, sku)
            reason = (
                f"App Service Plan idle: numberOfSites==0 in Resource Graph as of {self._snapshot}; "
                f"SKU {sku or 'unknown'} priced {monthly:.2f}/mo ({self._currency})."
            )
            out.append(self._finding(
                "idle_app_service_plans", plan, "microsoft.web/serverfarms", monthly,
                base_confidence=0.75,  # detection authoritative; ASP price is an estimate
                description=(f"App Service Plan '{plan.get('name')}' ({plan.get('skuName')}) hosts "
                            "no apps and is incurring idle compute charges."),
                recommendation="Delete the plan or deploy apps to it.",
                has_price=monthly > 0, debug_reason=reason, extra_details=plan,
            ))
        return out

    async def detect_app_service_rightsizing(self, plans: List[Dict]) -> List[Dict]:
        """Downsize an App Service Plan (hosting apps) to a smaller same-series SKU when CPU + memory
        stay consistently low. Mirrors VM rightsizing: peak (not average) over the window, BOTH signals
        required (a memory-bound plan mustn't be downsized on CPU alone), a conservative headroom
        ceiling on the projected target, and the saving = real price(current) − real price(target).
        Skips when either price is missing rather than guessing.
        """
        out: List[Dict] = []
        for plan in plans:
            max_cpu = plan.get("max_cpu_pct")
            max_mem = plan.get("max_memory_pct")
            dp = int(plan.get("metric_datapoints") or 0)
            window = int(plan.get("metric_window_days") or METRIC_WINDOW_DAYS)
            if max_cpu is None or dp == 0:
                continue  # no metrics → can't classify utilisation
            sku = plan.get("skuName") or ""
            region = plan.get("location") or "eastus"
            target = find_asp_downsize_target(sku, max_cpu, max_mem)
            if target is None:
                continue
            target_sku, t_cores, t_mem = target
            cur = _ASP_SPECS.get(sku.lower())
            cur_price = await self._pricing.get_app_service_plan_monthly_price(region, sku)
            target_price = await self._pricing.get_app_service_plan_monthly_price(region, target_sku)
            if not cur_price or not target_price:
                continue  # no real saving without both real prices
            monthly = round(max(0.0, cur_price - target_price), 2)
            if monthly <= 0:
                continue

            mem_available = max_mem is not None
            base_conf = metrics_confidence(dp, window)
            if not mem_available:
                base_conf = round(base_conf * 0.7, 2)
            mem_str = f"{max_mem}%" if mem_available else "unavailable"
            mem_caveat = "" if mem_available else " (memory could not be verified)"
            reason = (
                f"App Service Plan peak CPU {max_cpu}% and peak memory {mem_str} over {window} days"
                f"{mem_caveat} leave headroom to move {sku} → {target_sku} and stay under the "
                f"{DOWNSIZE_HEADROOM_CEILING}% ceiling; {cur_price:.2f}/mo → {target_price:.2f}/mo."
            )
            out.append(self._finding(
                "app_service_plan_rightsizing", plan, "microsoft.web/serverfarms", monthly,
                base_confidence=round(base_conf * 0.9, 2),
                description=(f"App Service Plan '{plan.get('name')}' ({sku}) peaked at {max_cpu}% CPU and "
                            f"{mem_str} memory over {window} days{mem_caveat} — it fits a smaller plan."),
                recommendation=f"Scale the plan down from {sku} to {target_sku}.",
                has_price=True, debug_reason=reason,
                extra_details={
                    "max_cpu": max_cpu, "peak_memory_used_pct": max_mem,
                    "memory_verified": mem_available, "cpu_datapoints": dp,
                    # Reuse the VM resize evidence panel (keys off current_sku/recommended_sku/vcpu).
                    "current_sku": sku, "recommended_sku": target_sku,
                    "current_monthly_price": cur_price, "recommended_monthly_price": target_price,
                    "current_vcpu": cur[1] if cur else None,
                    "current_memory_gb": cur[2] if cur else None,
                    "recommended_vcpu": t_cores, "recommended_memory_gb": t_mem,
                    "downsize_ceiling_pct": DOWNSIZE_HEADROOM_CEILING,
                },
            ))
        return out

    async def detect_sql_db_rightsizing(self, dbs: List[Dict]) -> List[Dict]:
        """Downsize a vCore SQL Database to fewer vCores when CPU, data IO AND log IO all stay low.

        SQL is stateful and performance-sensitive, so this is deliberately cautious: it runs ONLY where
        per-resource Cost Management data exists (never a list-price guess), requires EVERY measured
        load dimension to clear the headroom ceiling on the projected smaller size, and grounds the
        saving as `actual cost × (removed vCores / current vCores)`, capped at actual cost — so it
        tracks the real bill and can't exceed it. A recommendation to review, not an auto-action.
        """
        out: List[Dict] = []
        if not self._cost_map:
            return out  # grounded-only: without real cost we won't guess a SQL downsize
        for db in dbs:
            cpu = db.get("max_cpu_pct")
            data_io = db.get("max_data_io_pct")
            log_io = db.get("max_log_io_pct")
            dp = int(db.get("metric_datapoints") or 0)
            window = int(db.get("metric_window_days") or METRIC_WINDOW_DAYS)
            if cpu is None or dp == 0:
                continue
            current_vcores = int(db.get("vcores") or 0)
            actual = self._cost_map.get((db.get("id") or "").lower())
            if not actual or actual <= 0:
                continue
            target = find_sql_vcore_target(current_vcores, [cpu, data_io, log_io])
            if target is None:
                continue
            monthly = round(actual * (current_vcores - target) / current_vcores, 2)
            if monthly <= 0:
                continue

            tier = db.get("tier") or "vCore"
            current_label = f"{tier} {current_vcores} vCore"
            target_label = f"{tier} {target} vCore"
            io_str = (f"data IO {data_io}%, log IO {log_io}%"
                      if data_io is not None and log_io is not None else "IO metrics partial")
            base_conf = metrics_confidence(dp, window)
            reason = (
                f"SQL DB peak CPU {cpu}%, {io_str} over {window} days all leave headroom to drop "
                f"{current_vcores}→{target} vCores and stay under {DOWNSIZE_HEADROOM_CEILING}%; "
                f"saving = actual cost × {current_vcores - target}/{current_vcores}."
            )
            out.append(self._finding(
                "sql_db_rightsizing", db, "microsoft.sql/servers/databases", monthly,
                base_confidence=round(base_conf * 0.85, 2),  # stateful resource → a touch more cautious
                description=(f"SQL Database '{db.get('name')}' ({current_label}) peaked at {cpu}% CPU "
                            f"({io_str}) over {window} days — it fits fewer vCores."),
                recommendation=f"Scale down from {current_vcores} to {target} vCores after confirming peak workloads.",
                has_price=True, debug_reason=reason,
                grounded=True,
                extra_details={
                    "max_cpu": cpu, "max_data_io_pct": data_io, "max_log_io_pct": log_io,
                    "cpu_datapoints": dp,
                    # Reuse the VM/ASP resize evidence panel.
                    "current_sku": current_label, "recommended_sku": target_label,
                    "current_vcpu": current_vcores, "recommended_vcpu": target,
                    "downsize_ceiling_pct": DOWNSIZE_HEADROOM_CEILING,
                },
            ))
        return out

    async def detect_sql_mi_rightsizing(self, instances: List[Dict]) -> List[Dict]:
        """Downsize a vCore SQL Managed Instance to fewer vCores when CPU stays consistently low.

        Same cautious, grounded shape as SQL DB rightsizing, but MI exposes CPU (`avg_cpu_percent`) as
        its primary throttleable load signal (no per-DB IO-percent metrics), so the decision is CPU-based
        against the larger MI vCore ladder. Grounded-only; saving = actual × (removed / current), capped.
        """
        out: List[Dict] = []
        if not self._cost_map:
            return out
        for mi in instances:
            cpu = mi.get("max_cpu_pct")
            dp = int(mi.get("metric_datapoints") or 0)
            window = int(mi.get("metric_window_days") or METRIC_WINDOW_DAYS)
            if cpu is None or dp == 0:
                continue
            current_vcores = int(mi.get("vcores") or 0)
            actual = self._cost_map.get((mi.get("id") or "").lower())
            if not actual or actual <= 0:
                continue
            target = find_sql_vcore_target(current_vcores, [cpu], ladder=_SQL_MI_VCORE_LADDER)
            if target is None:
                continue
            monthly = round(actual * (current_vcores - target) / current_vcores, 2)
            if monthly <= 0:
                continue

            tier = mi.get("tier") or "vCore"
            current_label = f"{tier} {current_vcores} vCore"
            target_label = f"{tier} {target} vCore"
            base_conf = metrics_confidence(dp, window)
            reason = (
                f"SQL MI peak CPU {cpu}% over {window} days leaves headroom to drop "
                f"{current_vcores}→{target} vCores under {DOWNSIZE_HEADROOM_CEILING}%; "
                f"saving = actual cost × {current_vcores - target}/{current_vcores}."
            )
            out.append(self._finding(
                "sql_mi_rightsizing", mi, "microsoft.sql/managedinstances", monthly,
                base_confidence=round(base_conf * 0.85, 2),
                description=(f"SQL Managed Instance '{mi.get('name')}' ({current_label}) peaked at "
                            f"{cpu}% CPU over {window} days — it fits fewer vCores."),
                recommendation=f"Scale down from {current_vcores} to {target} vCores after confirming peak workloads.",
                has_price=True, debug_reason=reason, grounded=True,
                extra_details={
                    "max_cpu": cpu, "cpu_datapoints": dp,
                    "current_sku": current_label, "recommended_sku": target_label,
                    "current_vcpu": current_vcores, "recommended_vcpu": target,
                    "downsize_ceiling_pct": DOWNSIZE_HEADROOM_CEILING,
                },
            ))
        return out

    async def detect_disk_rightsizing(
        self, disks: List[Dict], exclude_vm_ids: Optional[set] = None,
    ) -> List[Dict]:
        """Downgrade an attached Premium SSD disk to Standard SSD when its peak IOPS AND throughput
        both stay well within Standard SSD's baseline (the 70% headroom ceiling). Requires BOTH signals
        — a wrong downgrade throttles the disk's I/O — so a disk with no metrics is left alone.

        Low IOPS/throughput does NOT by itself make a downgrade safe: databases and other latency-
        sensitive workloads need Premium's low, consistent latency even at trivial IOPS (a SQL log disk
        commits transactions on it). So disks on registered SQL VMs (`exclude_vm_ids`) are skipped
        outright, and the recommendation carries an explicit "not for latency-sensitive workloads" caveat
        for the cases we can't detect. Saving = real price(Premium, size) − real price(Standard SSD, size),
        capped at the disk's actual billed cost when known. Skips if either price is missing.
        """
        out: List[Dict] = []
        exclude_vm_ids = exclude_vm_ids or set()
        ceiling = DOWNSIZE_HEADROOM_CEILING / 100.0
        iops_ceiling = _STANDARD_SSD_BASELINE_IOPS * ceiling
        mbps_ceiling = _STANDARD_SSD_BASELINE_MBPS * ceiling
        for disk in disks:
            if (disk.get("managedBy") or "").lower() in exclude_vm_ids:
                continue  # attached to a SQL VM → needs Premium latency, don't recommend a downgrade
            peak_iops = disk.get("peak_iops")
            peak_mbps = disk.get("peak_mbps")
            dp = int(disk.get("metric_datapoints") or 0)
            window = int(disk.get("metric_window_days") or METRIC_WINDOW_DAYS)
            size = int(disk.get("sizeGB") or 0)
            if peak_iops is None or peak_mbps is None or dp == 0 or size <= 0:
                continue  # both I/O signals required → never a blind downgrade
            if peak_iops > iops_ceiling or peak_mbps > mbps_ceiling:
                continue  # too busy for Standard SSD
            region = disk.get("location") or "eastus"
            cur_sku = disk.get("skuName") or "Premium_LRS"
            try:
                premium_price = await self._pricing.get_managed_disk_monthly_price(region, cur_sku, size)
                standard_price = await self._pricing.get_managed_disk_monthly_price(
                    region, "StandardSSD_LRS", size)
            except PricingUnavailableError:
                continue
            if not premium_price or not standard_price:
                continue
            monthly = round(max(0.0, premium_price - standard_price), 2)
            actual = self._cost_map.get((disk.get("id") or "").lower())
            if actual and actual > 0:
                monthly = round(min(monthly, actual), 2)  # never claim more than the disk actually costs
            if monthly <= 0:
                continue

            base_conf = metrics_confidence(dp, window)
            reason = (
                f"Premium disk peaked at {peak_iops:.0f} IOPS / {peak_mbps:.0f} MB/s over {window} days "
                f"— within Standard SSD's ~{_STANDARD_SSD_BASELINE_IOPS} IOPS / "
                f"{_STANDARD_SSD_BASELINE_MBPS} MB/s baseline; {premium_price:.2f}/mo → {standard_price:.2f}/mo."
            )
            out.append(self._finding(
                "disk_rightsizing", disk, "microsoft.compute/disks", monthly,
                base_confidence=round(base_conf * 0.9, 2),
                description=(f"Managed disk '{disk.get('name')}' ({size} GB Premium SSD) peaked at only "
                            f"{peak_iops:.0f} IOPS / {peak_mbps:.0f} MB/s over {window} days — a throughput "
                            "level Standard SSD handles at lower cost."),
                recommendation=(f"If this disk does NOT back a latency-sensitive workload (databases, "
                                f"transaction logs, etc. need Premium's low latency even at low IOPS), change "
                                f"{cur_sku} → StandardSSD_LRS at the same size. Verify the workload first."),
                has_price=True, debug_reason=reason,
                grounded=bool(actual and actual > 0),
                extra_details={
                    "peak_iops": round(peak_iops, 1), "peak_mbps": round(peak_mbps, 1),
                    "cpu_datapoints": dp,
                    "current_sku": cur_sku, "recommended_sku": "StandardSSD_LRS",
                    "current_monthly_price": premium_price, "recommended_monthly_price": standard_price,
                    "sizeGB": size,
                },
            ))
        return out

    def detect_deallocated_vms(self, vms: List[Dict]) -> List[Dict]:
        """Deallocated/stopped VMs: compute stops billing, but the OS + data disks keep billing.

        The saving from removing the VM (and its disks) is the cost of those still-billing disks, read
        straight from Cost Management (the disks are attached to the VM, so they don't show up in the
        unattached-disk detector). Without per-resource cost data the disk cost can't be quantified, so
        the saving is 0 and the finding drops out (a zero-value line is noise in a savings report).
        """
        out = []
        sym = symbol(self._currency)
        for vm in vms:
            disk_ids: List[str] = []
            if vm.get("osDiskId"):
                disk_ids.append(vm["osDiskId"].lower())
            for dd in (vm.get("dataDisks") or []):
                did = ((dd or {}).get("managedDisk") or {}).get("id")
                if did:
                    disk_ids.append(did.lower())
            disk_cost = round(sum(self._cost_map.get(d, 0.0) for d in disk_ids), 2)
            n_disks = len(disk_ids)
            state = vm.get("powerState") or "deallocated"
            reason = (
                f"VM powerState=='{state}' in Resource Graph as of {self._snapshot}; compute stopped "
                f"but {n_disks} attached disk(s) still bill {sym}{disk_cost:.2f}/mo (Cost Management)."
            )
            out.append(self._finding(
                "deallocated_vms", vm, "microsoft.compute/virtualmachines", disk_cost,
                base_confidence=0.9,
                description=(f"VM '{vm.get('name')}' ({vm.get('vmSize')}) is {state}. Its {n_disks} "
                            f"disk{'s' if n_disks != 1 else ''} keep accruing storage cost while it's stopped."),
                recommendation="Delete the VM and its disks if it's no longer needed.",
                has_price=disk_cost > 0, debug_reason=reason,
                # Grounded in the disks' real cost, not the VM's ~0 compute — override the cap basis.
                grounded=disk_cost > 0, actual_cost_override=disk_cost if disk_cost > 0 else None,
                extra_details={**vm, "attached_disk_count": n_disks, "disk_monthly_cost": disk_cost},
            ))
        return out

    def detect_paused_sql_databases(self, dbs: List[Dict]) -> List[Dict]:
        out = []
        for db in dbs:
            reason = (
                f"SQL DB inactive: status=='{db.get('status')}' in Resource Graph as of "
                f"{self._snapshot}; storage still bills while paused."
            )
            out.append(self._finding(
                "paused_sql_databases", db, "microsoft.sql/servers/databases", 0.0,
                base_confidence=0.85,
                description=(f"SQL Database '{db.get('name')}' is '{db.get('status')}'. Storage "
                            "cost continues to accrue."),
                recommendation="Delete or archive the database if no longer required.",
                has_price=True, debug_reason=reason, extra_details=db,
            ))
        return out

    def detect_stopped_sql_managed_instances(self, mis: List[Dict]) -> List[Dict]:
        out = []
        for mi in mis:
            reason = (
                f"SQL MI stopped: state=='{mi.get('state')}' in Resource Graph as of {self._snapshot}; "
                "MI bills vCores continuously regardless of usage."
            )
            out.append(self._finding(
                "stopped_sql_managed_instances", mi, "microsoft.sql/managedinstances", 0.0,
                base_confidence=0.9,
                description=(f"SQL Managed Instance '{mi.get('name')}' is '{mi.get('state')}'. MI is "
                            "billed for vCores continuously."),
                recommendation="Delete the Managed Instance if it is no longer needed.",
                has_price=True, debug_reason=reason, extra_details=mi,
            ))
        return out

    # -- rule-driven orphan/waste (ARG-authoritative, broad coverage) ---------

    async def detect_orphans(self, bucket: str, rows: List[Dict]) -> List[Dict]:
        """Evaluate a rule-driven orphan bucket (snapshots, empty LBs, NAT gw, GRS vaults…).

        Load Balancer / NAT Gateway / Bastion are priced LIVE from the Retail Prices API (in the
        billing currency, with the dated estimate as a sanity-checked fallback). Snapshot and GRS
        vault stay estimates because their cost depends on data volume Resource Graph doesn't expose.
        """
        rule = ORPHAN_RULES.get(bucket)
        if rule is None:
            return []
        out = []
        for row in rows:
            region = row.get("location") or "eastus"
            if bucket == "empty_load_balancers":
                monthly = await self._pricing.get_load_balancer_monthly_price(region)
            elif bucket == "idle_nat_gateways":
                monthly = await self._pricing.get_nat_gateway_monthly_price(region)
            elif bucket == "bastion_hosts":
                monthly = await self._pricing.get_bastion_monthly_price(region, row.get("skuName") or "Basic")
            else:
                monthly = from_usd(rule.monthly_cost(row), self._currency)  # snapshot / vault estimate
            reason = (
                f"{CATEGORY_DISPLAY.get(rule.category, rule.category)}: matched by Resource Graph "
                f"state as of {self._snapshot}; estimated {monthly:.2f}/mo ({self._currency})."
            )
            out.append(self._finding(
                rule.category, row, rule.resource_type, monthly,
                base_confidence=rule.base_confidence,
                description=rule.describe(row),
                recommendation=rule.recommendation,
                has_price=True, debug_reason=reason, extra_details=row,
            ))
        return out

    # -- metrics-based (idle / downsize), keyed off PEAK CPU + PEAK MEMORY over 30d --

    async def detect_vm_utilisation_findings(self, vms: List[Dict]) -> List[Dict]:
        """Classify running VMs using BOTH peak CPU and peak memory over the window.

          both peaks < idle bars                → idle      → deallocate (savings = full cost)
          not idle, a smaller same-series SKU    → downsize  → recommend that EXACT target SKU;
            clears DOWNSIZE_HEADROOM_CEILING on              savings = real price(current) −
            both projected CPU and memory                    real price(target)
          no smaller SKU clears the ceiling      → well-used → no finding (left to Advisor)

        Peak (not average) CPU avoids flagging spiky/scheduled workloads as idle. Requiring BOTH
        CPU and memory (not CPU alone) avoids flagging a memory-bound VM (e.g. an in-memory cache)
        as idle just because it happens to be CPU-light. When memory couldn't be measured for a VM
        (no data from Azure Monitor), the CPU-only signal is still used but confidence is reduced
        and the finding is marked `memory_verified=False` — never silently treated as "0% memory".
        """
        out: List[Dict] = []
        cost_available = bool(self._cost_map)
        for vm in vms:
            max_cpu = vm.get("max_cpu")
            avg_cpu = vm.get("avg_cpu")
            cpu_datapoints = int(vm.get("cpu_datapoints") or 0)
            peak_mem = vm.get("peak_memory_used_pct")
            memory_available = bool(vm.get("memory_available"))
            window = int(vm.get("metric_window_days") or METRIC_WINDOW_DAYS)
            if max_cpu is None or cpu_datapoints == 0:
                continue  # no CPU metrics at all → cannot classify utilisation
            # A VM that isn't billing in the window (deallocated / demo box) yields no real saving
            # from resizing or shutting it down — skip it when per-resource cost is available.
            if cost_available and not self._cost_map.get((vm.get("id") or "").lower()):
                continue

            sku = vm.get("vmSize") or ""
            region = vm.get("location") or "eastus"
            try:
                payg = await self._pricing.get_vm_monthly_price(region, sku)
            except PricingUnavailableError:
                payg = None

            base_conf = metrics_confidence(cpu_datapoints, window)
            if not memory_available:
                base_conf = round(base_conf * 0.7, 2)  # memory unverified → reduced confidence
            avg_str = f"{avg_cpu}%" if avg_cpu is not None else "n/a"
            mem_str = f"{peak_mem}%" if peak_mem is not None else "unavailable"
            mem_caveat = "" if memory_available else " (memory could not be verified for this VM)"

            is_idle = max_cpu < IDLE_MAX_CPU and (
                memory_available and peak_mem is not None and peak_mem < IDLE_MAX_MEMORY_PCT
            )

            if is_idle:
                monthly = payg or 0.0
                reason = (
                    f"peak CPU {max_cpu}% and peak memory used {mem_str} over the last {window} days "
                    f"(avg CPU {avg_str}, {cpu_datapoints} data points) both stayed below the idle "
                    f"bars ({IDLE_MAX_CPU}% CPU / {IDLE_MAX_MEMORY_PCT}% memory) — the VM does "
                    f"effectively no work; {sku} priced ${monthly:.2f}/mo."
                )
                out.append(self._finding(
                    "idle_vms", vm, "microsoft.compute/virtualmachines", monthly,
                    base_confidence=base_conf,
                    description=(f"VM '{vm.get('name')}' ({sku}) peaked at just {max_cpu}% CPU and "
                                f"{mem_str} memory over {window} days — effectively idle."),
                    recommendation="Deallocate or delete this VM if it is no longer needed.",
                    has_price=payg is not None, debug_reason=reason,
                    extra_details={
                        "avg_cpu": avg_cpu, "max_cpu": max_cpu, "peak_memory_used_pct": peak_mem,
                        "memory_verified": memory_available, "cpu_datapoints": cpu_datapoints,
                    },
                ))
                continue

            target = find_downsize_target(sku, max_cpu, peak_mem if memory_available else None)
            if target is None:
                continue  # no smaller SKU has headroom → well-utilised, leave alone
            current_spec = get_spec(sku)

            try:
                target_price = await self._pricing.get_vm_monthly_price(region, target.sku)
            except PricingUnavailableError:
                target_price = None
            if payg is None or target_price is None:
                continue  # can't state a real saving without both real prices — skip rather than guess
            monthly = round(max(0.0, payg - target_price), 2)
            if monthly <= 0:
                continue  # target isn't actually cheaper (edge-case pricing) → nothing to recommend

            reason = (
                f"peak CPU {max_cpu}% and peak memory used {mem_str} over the last {window} days"
                f"{mem_caveat} leave headroom to move from {sku} to {target.sku} and stay under the "
                f"{DOWNSIZE_HEADROOM_CEILING}% ceiling on both; ${payg:.2f}/mo → ${target_price:.2f}/mo."
            )
            out.append(self._finding(
                "oversized_vms", vm, "microsoft.compute/virtualmachines", monthly,
                base_confidence=round(base_conf * 0.9, 2),
                description=(f"VM '{vm.get('name')}' ({sku}) peaked at {max_cpu}% CPU and {mem_str} "
                            f"memory over {window} days{mem_caveat} — comfortably fits on a smaller "
                            f"SKU."),
                recommendation=f"Resize from {sku} to {target.sku}.",
                has_price=True, debug_reason=reason,
                extra_details={
                    "avg_cpu": avg_cpu, "max_cpu": max_cpu, "peak_memory_used_pct": peak_mem,
                    "memory_verified": memory_available, "cpu_datapoints": cpu_datapoints,
                    "current_sku": sku, "recommended_sku": target.sku,
                    "current_monthly_price": payg, "recommended_monthly_price": target_price,
                    # Absolute specs power the before/after visual on the frontend.
                    "current_vcpu": current_spec.vcpu if current_spec else None,
                    "current_memory_gb": current_spec.memory_gb if current_spec else None,
                    "recommended_vcpu": target.vcpu,
                    "recommended_memory_gb": target.memory_gb,
                    "downsize_ceiling_pct": DOWNSIZE_HEADROOM_CEILING,
                },
            ))
        return out

    # -- commitments: Reserved Instances + Savings Plan (VMs) ------------------

    def _aggregate_commitment_finding(
        self, category: str, kind: str, items: List[Dict], *,
        source: str, base_confidence: float, unit: str = "SKU", grounded: bool = False,
        context_note: str = "",
    ) -> Optional[Dict]:
        """Roll many per-SKU/VM commitment items into ONE finding.

        `kind` is "Reserved Instance". Each item carries `s1` (1-year) and optional `s3` (3-year)
        monthly saving. The **best case (3-year, the deepest discount) is the counted headline**; the
        1-year option is shown alongside so the client can pick the shorter commitment. Resource-less
        (`id=None`) so it isn't collapsed by the per-resource dedupe. `context_note` is appended to the
        description (e.g. the production-targeting rationale for VMs).
        """
        one_total = 0.0
        three_total = 0.0
        has_3yr = False
        any_estimated = False
        ui_items: List[Dict] = []
        for it in items:
            s1 = it.get("s1")
            s3 = it.get("s3")
            best = s3 if s3 is not None else s1  # deepest available discount for this item
            if best is None or best <= 0:
                continue
            one_total += (s1 if s1 is not None else s3)
            three_total += best
            if s3 is not None:
                has_3yr = True
            any_estimated = any_estimated or bool(it.get("ri_price_estimated"))
            ui_items.append({
                "name": it.get("name"), "sku": it.get("sku"), "region": it.get("region"),
                "quantity": it.get("quantity") or 0,
                "monthly_savings": round(s1, 2) if s1 is not None else round(best, 2),
                "monthly_savings_3yr": round(s3, 2) if s3 is not None else None,
                "monthly_ondemand": it.get("ondemand"), "monthly_reserved": it.get("reserved"),
                "environment": it.get("environment"),
            })
        if not ui_items:
            return None

        one_total = round(one_total, 2)
        three_total = round(three_total, 2)
        headline = three_total if has_3yr else one_total  # best case = 3-year when available
        ui_items.sort(key=lambda x: -(x["monthly_savings_3yr"] or x["monthly_savings"]))
        n = len(ui_items)
        plural = "s" if n != 1 else ""
        shown = ", ".join(str(x["sku"]) for x in ui_items[:6] if x["sku"])
        if n > 6:
            shown += f", +{n - 6} more"

        # Best case first (3-year), then the 1-year alternative.
        options = []
        if has_3yr:
            options.append({"label": f"3-year {kind}", "monthly_savings": three_total})
        options.append({"label": f"1-year {kind}", "monthly_savings": one_total})

        synthetic = {
            "id": None,
            "name": f"{kind}s — {n} {unit}{plural}",
            "subscriptionId": items[0].get("subscription_id") if items else None,
            "resourceGroup": None,
        }
        sym = symbol(self._currency)
        rate_note = (
            f"best rate about {sym}{three_total:,.0f}/mo on a 3-year term, {sym}{one_total:,.0f}/mo on 1-year"
            if has_3yr else f"about {sym}{one_total:,.0f}/mo on a 1-year term"
        )
        reason = (
            f"Aggregated {n} {kind} candidate{plural} ({source}); best case 3-year "
            f"{sym}{three_total:.2f}/mo, 1-year {sym}{one_total:.2f}/mo. {unit}s: {shown}."
        )
        return self._finding(
            category, synthetic, "microsoft.consumption/reservationrecommendations", headline,
            base_confidence=base_confidence,
            description=(
                f"{n} {unit}{plural} are candidates to reserve ({shown}). A 1- or 3-year {kind} locks in "
                f"a lower rate than pay-as-you-go — {rate_note}.{context_note}"
            ),
            recommendation=(
                f"Purchase {'3-year' if has_3yr else '1-year'} {kind}s for the {unit.lower()}{plural} "
                f"listed for the best rate"
                + (", or a 1-year term for more flexibility." if has_3yr else ".")
            ),
            has_price=True, debug_reason=reason, grounded=grounded,
            extra_details={
                "aggregate": True, "source": source, "kind": kind,
                "reservation_options": options, "reservation_items": ui_items,
                "item_count": n, "total_1yr_monthly": one_total,
                "total_3yr_monthly": three_total if has_3yr else None,
                "ri_price_estimated": any_estimated,
            },
        )

    async def detect_vm_commitments(
        self, vms: List[Dict], windows_no_ahb_ids: Optional[set] = None,
    ) -> List[Dict]:
        """Recommend Reserved Instances for **production** VMs (Savings Plans are not recommended).

        A Reserved Instance suits a long-lived workload, and a provisioned production VM almost always
        is one — so we recommend an RI for every running prod VM and **skip dev/test/staging** (short-
        lived, not worth a 1–3 year commitment). Environment is read from tags first, then the VM/RG
        name; unclassifiable VMs are **assumed production** and flagged. Idle VMs and Advisor-covered
        VMs are still excluded (you don't reserve something you're deleting).

        The saving is `compute_base × RI discount`, NOT the whole cost — the base is the COMPUTE portion
        of the VM's actual bill (the Windows licence is stripped for non-AHB Windows VMs so RI and AHB
        never overlap). When a SKU's retail RI price can't be fetched, a typical RI discount is used and
        the item is flagged estimated (so a prod VM is never silently dropped).
        """
        if self._reservation_basis == "advisor":
            return []
        windows_no_ahb_ids = windows_no_ahb_ids or set()
        ri_items: List[Dict] = []
        all_steady = True
        cost_available = bool(self._cost_map)
        for vm in vms:
            max_cpu = vm.get("max_cpu")
            peak_mem = vm.get("peak_memory_used_pct")
            mem_ok = bool(vm.get("memory_available"))
            # Skip genuinely idle VMs — you delete those, not reserve them (idle detector covers).
            if (max_cpu is not None and max_cpu < IDLE_MAX_CPU
                    and mem_ok and peak_mem is not None and peak_mem < IDLE_MAX_MEMORY_PCT):
                continue
            # Environment: recommend RI for prod (and unclassifiable-assumed-prod); skip clear dev/test.
            env, env_reason = _vm_environment(vm)
            if env == "nonprod":
                continue
            rid = vm.get("id")
            if rid and self._advisor_index.get(rid.lower()):
                continue  # Advisor wins
            actual = self._cost_map.get((rid or "").lower())
            # No point reserving a VM that isn't billing (deallocated / demo box not in the window).
            if cost_available and (not actual or actual <= 0):
                continue

            sku = vm.get("vmSize") or ""
            region = vm.get("location") or "eastus"
            try:
                payg = await self._pricing.get_vm_monthly_price(region, sku)
                windows = await self._pricing.get_vm_windows_monthly_price(region, sku)
                ri1 = await self._pricing.get_vm_reserved_monthly_price(region, sku, "1 Year")
                ri3 = await self._pricing.get_vm_reserved_monthly_price(region, sku, "3 Years")
            except PricingUnavailableError:
                continue
            if not payg:
                continue

            # Discount RATIOS from retail (currency-independent). If a SKU's retail RI price is missing,
            # fall back to typical Azure VM RI discounts so a prod VM still gets a (flagged) recommendation.
            def _ratio(price):
                return (payg - price) / payg if price and payg and payg > price else None
            r1, r3 = _ratio(ri1), _ratio(ri3)
            ri_estimated = False
            if r1 is None:
                r1, ri_estimated = _RI_DEFAULT_DISCOUNT_1YR, True
            if r3 is None:
                r3, ri_estimated = _RI_DEFAULT_DISCOUNT_3YR, True

            # Base = the COMPUTE portion of the VM's actual cost (Windows licence stripped for non-AHB
            # Windows VMs so RI never overlaps AHB). No billing data → retail run-rate.
            is_win_no_ahb = bool(rid and rid.lower() in windows_no_ahb_ids)
            compute_fraction = (payg / windows) if (is_win_no_ahb and windows and windows > payg) else 1.0
            if actual is not None and actual > 0:
                base = round(actual * compute_fraction, 2)
                ondemand = round(actual, 2)
            else:
                base = payg
                ondemand = payg

            s1 = round(base * r1, 2) if r1 else None
            s3 = round(base * r3, 2) if r3 else None
            if (s1 is None or s1 <= 0) and (s3 is None or s3 <= 0):
                continue

            # Steadiness is no longer a GATE (a prod VM gets an RI regardless) — it only tunes
            # confidence: a VM billed consistently across months (or with full metric coverage) is a
            # safer commit than one with sparse/erratic data.
            cons = self._consistency.get((rid or "").lower())
            datapoints = int(vm.get("cpu_datapoints") or 0)
            window = int(vm.get("metric_window_days") or METRIC_WINDOW_DAYS)
            if cons and cons.get("billed_months"):
                steady = bool(cons.get("stable"))
            else:
                steady = bool(window and datapoints >= 0.8 * window)
            all_steady = all_steady and steady
            ri_items.append({
                "name": vm.get("name"), "sku": sku, "region": region, "quantity": 1,
                "ondemand": ondemand, "subscription_id": vm.get("subscriptionId"),
                "reserved": round(ondemand - (s1 or 0), 2),
                "billed_months": cons.get("billed_months") if cons else None,
                "cost_stable": cons.get("stable") if cons else None,
                "compute_base": round(base, 2), "actual_monthly_cost": round(actual, 2) if actual else None,
                "discount_1yr": round(r1, 4) if r1 else None,
                "discount_3yr": round(r3, 4) if r3 else None,
                "grounded": actual is not None and actual > 0,
                "s1": s1, "s3": s3,
                "environment": env, "env_reason": env_reason, "ri_price_estimated": ri_estimated,
            })

        if not ri_items:
            return []
        assumed = sum(1 for it in ri_items if it["environment"] == "unknown")
        note = (f" Recommended for production VMs; {assumed} untagged VM{'s' if assumed != 1 else ''} "
                "assumed production — verify before committing." if assumed else
                " Recommended for production VMs.")
        conf = 0.85 if all_steady else 0.65
        f = self._aggregate_commitment_finding(
            "ri_vm", "Reserved Instance", ri_items, source="retail_estimate",
            base_confidence=conf, unit="VM", grounded=cost_available, context_note=note)
        return [f] if f else []

    # -- commitments from Azure's own reservation engine (authoritative) -------

    def commitments_from_recommendations(self, groups: List[Dict]) -> List[Dict]:
        """Build reservation findings from parsed Consumption `reservationRecommendations`.

        These are SKU-level *purchase* recommendations (a reservation covers matching resources in a
        region), computed by Azure on real usage at the customer's real prices, excluding reservations
        already owned. Used here for **non-VM** resource types only (SQL / Cosmos / App Service / Files /
        Disks); **VMs are handled by `detect_vm_commitments`** (production-targeted, not gated on Azure's
        steadiness check). Grouped into ONE finding per finding-category. Resource-less (escapes dedupe).
        """
        by_category: Dict[str, List[Dict]] = {}
        for g in groups:
            if g.get("category") in ("ri_vm", "savings_plan_vm"):
                continue  # VMs are covered by detect_vm_commitments (production-targeted)
            terms = g.get("terms", {})
            p1 = terms.get("P1Y")
            p3 = terms.get("P3Y")
            head = p1 or p3
            if not head or head.get("monthly_savings", 0) <= 0:
                continue
            by_category.setdefault(g["category"], []).append({
                "name": g.get("sku"), "sku": g.get("sku"), "region": g.get("region"),
                "quantity": int(head.get("quantity") or 0),
                "s1": p1["monthly_savings"] if p1 else None,
                "s3": p3["monthly_savings"] if p3 else None,
                "ondemand": head.get("monthly_ondemand"), "reserved": head.get("monthly_reserved"),
                "subscription_id": g.get("subscription_id"),
            })

        out: List[Dict] = []
        for category, items in by_category.items():
            f = self._aggregate_commitment_finding(
                category, "Reserved Instance", items,
                source="azure_reservation_recommendations", base_confidence=0.9, unit="SKU",
                grounded=True)  # Azure computes these on real usage at real prices — authoritative
            if f:
                out.append(f)
        return out

    # -- Windows Azure Hybrid Benefit (one classified finding) -----------------

    async def detect_windows_ahb(
        self, vms: List[Dict], exclude_ids: Optional[set] = None,
    ) -> List[Dict]:
        """Roll all AHB-eligible Windows VMs into ONE classified finding.

        Rather than one noisy finding per VM, we list every running Windows VM not using Azure
        Hybrid Benefit and sum the licence delta (Windows − compute-only price). Deliberately resource-less
        (`id=None`) so it isn't collapsed by the per-resource dedupe: AHB saves the *licence*, which
        is additive with any RI/downsize action taken on the same VM's *compute*. `exclude_ids` drops
        VMs already recommended for deletion (idle/stopped) so their licence isn't double-counted.
        """
        exclude_ids = exclude_ids or set()
        cost_available = bool(self._cost_map)  # per-resource Cost Management data present?

        # Pass 1 — gather each eligible VM's prices + vCPUs, and sample the per-vCore licence.
        rows: List[Dict] = []
        per_vcore_samples: List[float] = []
        for vm in vms:
            if (vm.get("id") or "").lower() in exclude_ids:
                continue  # VM is being deleted (idle/stopped) → no licence to save
            sku = vm.get("vmSize") or ""
            region = vm.get("location") or "eastus"
            actual = self._cost_map.get((vm.get("id") or "").lower())
            # You can't save the Windows licence on a VM you aren't paying for. With per-resource
            # billing, skip VMs showing no measured cost (deallocated / demo boxes); only without it
            # do we fall back to the raw retail delta.
            if cost_available and (not actual or actual <= 0):
                continue
            try:
                windows = await self._pricing.get_vm_windows_monthly_price(region, sku)
                # Compute-only (post-AHB) rate = a same-size Linux VM: identical hardware, no licence.
                compute_only = await self._pricing.get_vm_monthly_price(region, sku)
            except PricingUnavailableError:
                continue
            if not compute_only:
                continue
            vcpu = _vcpus(sku)
            rows.append({"vm": vm, "sku": sku, "region": region, "actual": actual,
                         "windows": windows, "compute_only": compute_only, "vcpu": vcpu})
            # Sample the per-vCore licence only where the Windows fetch looks sane (clearly > Linux).
            if windows and vcpu and windows > compute_only:
                per_vcore_samples.append((windows - compute_only) / vcpu)
        if not rows:
            return []

        # The Windows Server licence is a flat per-vCore charge — it should be identical per vCore on
        # every VM. Estimate it robustly with the MEDIAN of the samples: that neutralises a bad
        # per-SKU price fetch (some B-series return a Windows price barely above Linux, which would
        # otherwise understate their AHB) without disturbing the VMs that priced correctly.
        per_vcore_licence = _median(per_vcore_samples)

        eligible: List[Dict] = []
        total = 0.0
        sub_id: Optional[str] = None
        for r in rows:
            vm, windows, compute_only = r["vm"], r["windows"], r["compute_only"]
            vcpu, actual = r["vcpu"], r["actual"]
            # Licence = vCPUs × the environment's per-vCore rate; fall back to this VM's own retail
            # delta only when it can't be sized or there's no per-vCore estimate.
            if per_vcore_licence is not None and vcpu:
                licence = vcpu * per_vcore_licence
                windows_eff = compute_only + licence
            elif windows and windows > compute_only:
                licence = windows - compute_only
                windows_eff = windows
            else:
                continue
            lic_fraction = max(0.0, licence / windows_eff) if windows_eff else 0.0
            if lic_fraction <= 0:
                continue
            # Take the licence as a fraction of the full Windows price, then apply it to the VM's ACTUAL
            # cost so the saving tracks real, part-time spend instead of full-time list price.
            if cost_available:
                monthly = round(actual * lic_fraction, 2)
                grounded = True
            else:
                monthly = round(licence, 2)
                grounded = False
            if monthly <= 0:
                continue
            sub_id = sub_id or vm.get("subscriptionId")
            eligible.append({
                "name": vm.get("name"), "sku": r["sku"], "region": r["region"],
                "monthly_savings": monthly, "resource_id": vm.get("id"),
                "actual_cost_based": grounded,
                # Audit trail — verifiable inputs. licence = vCPUs × per-vCore rate (median-calibrated);
                # windows_price = compute_only + licence; saving = (grounded ? actual : licence) × fraction.
                "windows_price": round(windows_eff, 2), "compute_only_price": round(compute_only, 2),
                "licence_charge": round(licence, 2), "licence_fraction": round(lic_fraction, 4),
                "vcpu": vcpu, "actual_monthly_cost": round(actual, 2) if actual else None,
            })
            total += monthly
        if not eligible:
            return []

        total = round(total, 2)
        eligible.sort(key=lambda e: -e["monthly_savings"])
        shown = ", ".join(e["name"] for e in eligible[:6] if e["name"])
        if len(eligible) > 6:
            shown += f", +{len(eligible) - 6} more"
        n = len(eligible)
        synthetic = {
            "id": None, "name": f"{n} Windows VM{'s' if n != 1 else ''} eligible for AHB",
            "subscriptionId": sub_id, "resourceGroup": None,
        }
        grounded_count = sum(1 for e in eligible if e.get("actual_cost_based"))
        reason = (
            f"{n} running Windows VMs without licenseType=Windows_Server; licence = vCPUs × per-vCore "
            f"rate (median-calibrated across the fleet, so a bad per-SKU fetch can't skew it), applied "
            f"as a fraction of actual cost for {grounded_count}/{n}; ${total:.2f}/mo across: {shown}."
        )
        return [self._finding(
            "windows_ahb", synthetic, "microsoft.compute/virtualmachines", total,
            base_confidence=0.7,
            description=(
                f"{n} Windows VM{'s' if n != 1 else ''} pay Azure's Windows Server licence on top of "
                f"compute ({shown}). Azure Hybrid Benefit removes that charge if you already own Windows "
                "Server licences with Software Assurance — the saving shown is that licence portion."
            ),
            recommendation=("Apply Azure Hybrid Benefit (licenseType=Windows_Server) to the VMs you "
                            "have spare Windows Server licences (with Software Assurance) for; leave the "
                            "rest on pay-as-you-go."),
            has_price=True, debug_reason=reason,
            # Grounded when every eligible VM's saving came from its actual bill (not the retail delta).
            grounded=cost_available and grounded_count == n,
            extra_details={"eligible_vms": eligible, "eligible_count": n},
        )]

    # -- SQL Server Azure Hybrid Benefit (one classified finding) ---------------

    async def detect_sql_ahb(
        self, sql_resources: List[Dict], exclude_ids: Optional[set] = None,
    ) -> List[Dict]:
        """Roll all AHB-eligible vCore SQL (Databases + Managed Instances) into ONE classified finding.

        Azure Hybrid Benefit lets you apply owned SQL Server licences (with Software Assurance) to vCore
        SQL, switching licenseType LicenseIncluded → BasePrice. It removes only the SQL Server *licence*
        component — a fixed per-vCore charge that's the same dollar amount across tiers — so the saving
        is `vCores × per-vCore licence`, bounded by the resource's actual billed cost. Isolating the
        licence keeps storage/backup out of the figure so it can't over-state. Resource-less (`id=None`)
        so it escapes the per-resource dedupe: the licence is additive with any reservation on compute.
        `exclude_ids` drops paused/stopped SQL (no billable compute → no licence to save).
        """
        exclude_ids = exclude_ids or set()
        per_vcore = from_usd(SQL_AHB_LICENCE_PER_VCORE_MONTHLY_USD, self._currency)
        eligible: List[Dict] = []
        total = 0.0
        sub_id: Optional[str] = None
        cost_available = bool(self._cost_map)
        grounded_count = 0
        for r in sql_resources:
            rid = (r.get("id") or "").lower()
            if rid in exclude_ids:
                continue
            vcores = int(r.get("vcores") or 0)
            if vcores <= 0:
                continue
            actual = self._cost_map.get(rid)
            # You can't save the licence on a resource you aren't billing for. With per-resource cost
            # data, skip SQL that shows no measured spend; only without it do we show the raw estimate.
            if cost_available and (not actual or actual <= 0):
                continue
            licence = round(vcores * per_vcore, 2)
            if cost_available:
                monthly = round(min(licence, actual), 2)  # a saving can't exceed what you pay
                grounded = True
            else:
                monthly = licence
                grounded = False
            if monthly <= 0:
                continue
            grounded_count += 1 if grounded else 0
            sub_id = sub_id or r.get("subscriptionId")
            eligible.append({
                "name": r.get("name"), "sku": r.get("skuName") or r.get("tier"),
                "region": r.get("location"), "monthly_savings": monthly,
                "resource_id": r.get("id"), "actual_cost_based": grounded,
                # Audit trail: saving = vCores × per-vCore licence, capped at actual.
                "vcores": vcores, "per_vcore_licence": round(per_vcore, 2),
                "actual_monthly_cost": round(actual, 2) if actual else None,
            })
            total += monthly
        if not eligible:
            return []

        total = round(total, 2)
        eligible.sort(key=lambda e: -e["monthly_savings"])
        shown = ", ".join(e["name"] for e in eligible[:6] if e["name"])
        if len(eligible) > 6:
            shown += f", +{len(eligible) - 6} more"
        n = len(eligible)
        synthetic = {
            "id": None, "name": f"{n} SQL resource{'s' if n != 1 else ''} eligible for SQL AHB",
            "subscriptionId": sub_id, "resourceGroup": None,
        }
        reason = (
            f"{n} vCore SQL resources with licenseType=LicenseIncluded; saving = vCores × "
            f"${SQL_AHB_LICENCE_PER_VCORE_MONTHLY_USD:.0f}/vCore licence, capped at actual cost for "
            f"{grounded_count}/{n}; total ${total:.2f}/mo across: {shown}."
        )
        return [self._finding(
            "sql_ahb", synthetic, "microsoft.sql/servers/databases", total,
            base_confidence=0.7,
            description=(
                f"{n} vCore SQL {'resource' if n == 1 else 'resources'} (Databases/Managed Instances) "
                f"pay Azure's SQL Server licence on top of compute ({shown}). Azure Hybrid Benefit removes "
                "that charge if you already own SQL Server licences with Software Assurance — the saving "
                "shown is that licence portion."
            ),
            recommendation=("Set licenseType to BasePrice (Azure Hybrid Benefit) on the vCore SQL "
                            "resources you have spare SQL Server licences (with Software Assurance) for; "
                            "leave the rest on pay-as-you-go."),
            has_price=True, debug_reason=reason,
            grounded=cost_available and grounded_count == n,
            extra_details={"eligible_vms": eligible, "eligible_count": n, "licence_kind": "SQL Server"},
        )]

    # -- Advisor-native recommendations ----------------------------------------

    def advisor_findings(self, recommendations: List[Dict]) -> List[Dict]:
        """Turn Azure Advisor cost recs into findings, re-scored + validated consistently."""
        out: List[Dict] = []
        seen: set = set()
        for rec in recommendations:
            props = rec.get("properties", {})
            ext = props.get("extendedProperties", {})
            short = props.get("shortDescription", {})
            meta = props.get("resourceMetadata", {})
            resource_id = meta.get("resourceId", "")
            key = f"advisor:{resource_id}:{short.get('problem','')}"
            if key in seen:
                continue
            seen.add(key)

            monthly, _ = _extract_savings(ext)
            impact = props.get("impact", "Medium")
            sub_id, rg = _parse_ids(resource_id) if resource_id else ("", "")
            resource = {
                "id": resource_id or None,
                "name": props.get("impactedValue") or (resource_id.split("/")[-1] if resource_id else None),
                "subscriptionId": sub_id or props.get("subscriptionId", ""),
                "resourceGroup": rg,
            }
            reason = (
                f"Azure Advisor rec '{rec.get('id','')}' matched by resource id; Advisor savings "
                f"estimate ${monthly:.2f}/mo, impact={impact}."
            )
            f = self._finding(
                "advisor_cost", resource, props.get("impactedField", ""), monthly,
                base_confidence=0.85,  # Azure-computed
                description=short.get("problem", "Azure Advisor cost recommendation."),
                recommendation=short.get("solution", "Follow the Azure Advisor recommendation."),
                has_price=True, advisor_impact=impact, debug_reason=reason,
                extra_details={"impact": impact, "extended_properties": ext},
            )
            # Advisor rec is itself the correlation id.
            f["advisor_recommendation_id"] = rec.get("id")
            out.append(f)
        return out
