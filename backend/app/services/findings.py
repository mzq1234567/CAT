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
from .currency import symbol, to_usd
from .financial_evidence import (
    ACTUAL_BILLED_COST,
    AUTHORITATIVE_RETAIL_PRICE,
    POTENTIAL_SAVINGS,
    QUANTIFIED,
    QUANTIFIED_SAVINGS,
    REVIEW,
    UNQUANTIFIED,
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

# The ratio actual_monthly_cost / licence-free (Linux) monthly list price is a VM's EFFECTIVE UPTIME —
# the share of the month it actually ran at its normal rate. A mostly-deallocated lab/dev VM legitimately
# bills only a small fraction of a full month; that's LOW UTILISATION, not bad data, and a saving grounded
# in actual cost scales down with it correctly (so it must NOT be flagged as suspect). Only when that ratio
# is NEGLIGIBLE — compute is effectively free — does it signal a sponsored/credited/trial subscription or a
# billing-scale/currency mismatch: a genuine anomaly worth flagging loudly.
ANOMALOUS_UPTIME_FRACTION = 0.01   # < ~1% effective uptime (≈7h/mo) → compute ≈ free → data is suspect
LOW_UTILISATION_FRACTION = 0.5     # < 50% effective uptime → VM runs part-time (informational, not alarming)

# Sponsored/credited-subscription guard for FLAT-RATE resources (disk / IP / LB / NAT / Bastion): when a
# resource that always bills at a fixed rate is billed at less than this fraction of its list price, the
# cost is effectively free — an Azure Sponsorship/credit (or a currency-scale mismatch), not the customer's
# real economics. The resource still has economic VALUE (≈ its list price), but the customer's realisable
# saving CANNOT be quantified from a near-zero bill, and we must not imply the resource is worthless. Such
# a finding becomes REVIEW (reference price shown) rather than a tiny, misleading quantified saving.
SPONSORED_COST_FRACTION = 0.01

# Minimum observed billing span before a run-rate (partial-billing) figure may be annualised into a
# confident saving. Below this, a few days of spend is NOT enough evidence to project a reliable annual
# number, so such findings become REVIEW ("insufficient billing history") instead of a quantified saving.
#
# IMPORTANT: this is THIS TOOL'S OWN conservative financial-integrity validation rule — it is NOT an
# Azure or Azure Advisor requirement, and Microsoft does not define a 14-day minimum anywhere. It is a
# threshold we chose to guarantee we never present a confident annual saving from too little billing
# history; tune it to appetite. (Azure's own 7/30/60-day windows are a SEPARATE concept — they're the
# reservation-recommendation look-back, set in azure_client.py, not this annualisation guard.)
MIN_BILLING_DAYS_FOR_ANNUAL = 14

# Conditional savings (Azure Hybrid Benefit) are realised ONLY if the customer already owns eligible
# licences, so they're kept OUT of the headline/realisable savings roll-up and surfaced separately as
# "potential". Single source of truth for that classification, shared by the persistence + API layers.
CONDITIONAL_CATEGORIES = frozenset({"windows_ahb", "sql_ahb"})


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

# ── Methodology disclosure ────────────────────────────────────────────────────────
# The utilisation bars above (idle CPU/memory, the downsize headroom ceiling, the Standard SSD
# baselines) are chosen BY THIS ASSESSMENT as a conservative, industry-standard practice — they are
# NOT defined by Microsoft. Every threshold-driven finding appends one of these disclosures to its
# recommendation so a client never mistakes them for an official Azure recommendation. (The pricing in
# these findings is still real: live Retail Prices, capped at the resource's actual billed cost. Where
# Azure Advisor independently flags the same resource, that corroboration raises the finding's
# confidence — see FindingsEngine._finding.)
_IDLE_METHODOLOGY = (
    f" Assessment methodology (not an Azure-defined rule): flagged because peak CPU stayed below "
    f"{IDLE_MAX_CPU}% and peak memory below {IDLE_MAX_MEMORY_PCT}% over the metric window; confirm the "
    "VM is genuinely unused before acting."
)
_RIGHTSIZE_METHODOLOGY = (
    f" Assessment methodology (not an Azure-defined rule): the target size is the smallest that keeps "
    f"projected peak utilisation under a {DOWNSIZE_HEADROOM_CEILING}% headroom ceiling; verify against "
    "your peak workloads before applying."
)
_DISK_METHODOLOGY = (
    f" Assessment methodology (not an Azure-defined rule): flagged because peak IOPS and throughput "
    f"stayed within ~{DOWNSIZE_HEADROOM_CEILING}% of Standard SSD's ~{_STANDARD_SSD_BASELINE_IOPS} IOPS "
    f"/ {_STANDARD_SSD_BASELINE_MBPS} MB/s baseline."
)

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
    "vm_metrics_unavailable": "VMs — Utilisation Metrics Unavailable",
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
# NONE of these carry a hardcoded price. Load Balancer / NAT Gateway / Bastion are priced LIVE from
# the Retail Prices API; orphaned snapshots are grounded in the snapshot's ACTUAL Cost Management
# billed cost (see detect_orphans). A bucket with no authoritative price for a row skips that row —
# a saving is never fabricated from a per-GB or flat estimate.
#
# The geo-redundant-vault (GRS→LRS) finding was REMOVED in the accuracy audit: its saving is the
# geo-redundancy premium on backup *storage*, which can't be isolated from the vault's total bill
# without an assumption, and Resource Graph doesn't expose the backup volume — so any dollar figure
# would be fabricated. Detection stays in the KQL layer, but no quantified saving is produced.


@dataclass(frozen=True)
class OrphanRule:
    category: str
    resource_type: str
    recommendation: str
    describe: Callable[[Dict], str]
    base_confidence: float = 0.9


def _short_date(row: Dict) -> str:
    return str(row.get("timeCreated") or "")[:10] or "unknown date"


ORPHAN_RULES: Dict[str, OrphanRule] = {
    "orphaned_snapshots": OrphanRule(
        "orphaned_snapshots", "microsoft.compute/snapshots",
        "Delete snapshots no longer needed for recovery or compliance.",
        lambda r: (f"Snapshot '{r.get('name')}' ({int(r.get('diskSizeGB') or 0)} GB, created "
                   f"{_short_date(r)}) is retained and accruing storage cost."),
        0.85,
    ),
    "empty_load_balancers": OrphanRule(
        "empty_load_balancers", "microsoft.network/loadbalancers",
        "Delete the load balancer if it is not routing traffic.",
        lambda r: (f"Standard Load Balancer '{r.get('name')}' has no backend pool — it bills "
                   "without balancing anything."),
        0.85,
    ),
    "idle_nat_gateways": OrphanRule(
        "idle_nat_gateways", "microsoft.network/natgateways",
        "Delete the NAT gateway if no subnet uses it.",
        lambda r: (f"NAT Gateway '{r.get('name')}' is not associated with any subnet, yet bills a "
                   "fixed hourly rate."),
        0.85,
    ),
    "bastion_hosts": OrphanRule(
        "bastion_hosts", "microsoft.network/bastionhosts",
        "Confirm Bastion is still needed, or deallocate it when not in use.",
        lambda r: (f"Azure Bastion '{r.get('name')}' ({r.get('skuName') or 'Standard'}) is a "
                   "fixed-cost resource that bills whether or not it's used — verify it is still required."),
        0.6,
    ),
}


# ── Scoring helpers (pure) ───────────────────────────────────────────────────────

def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def severity_from_savings(monthly: float, advisor_impact: str = "", currency: str = "USD") -> str:
    # Severity tracks SAVINGS MAGNITUDE only, so the ordering is always consistent — a smaller finding
    # can never be flagged higher-impact than a larger one. (Azure Advisor's own High/Medium/Low rating
    # is preserved on the finding's details and shown as a separate "Advisor" tag in the UI, but it no
    # longer inflates the severity chip — otherwise a ₹432/yr Advisor rec could read "High" next to a
    # ₹1.6K RI reading "Low".) `advisor_impact` is accepted for backwards compatibility and ignored.
    # Bands are defined in USD; convert the (billing-currency) saving so INR/GBP/etc. aren't all flagged
    # critical just because the number is larger/smaller in absolute terms.
    usd = to_usd(monthly, currency)
    return (
        "critical" if usd >= 300
        else "high" if usd >= 100
        else "medium" if usd >= 20
        else "low"
    )


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
        measured_monthly_spend: Optional[float] = None,
        cost_basis: Optional[Dict[str, Dict]] = None,
        billing_span_days: Optional[int] = None,
    ):
        self._pricing = pricing
        self._cost_map = cost_map or {}
        self._advisor_index = advisor_index or {}
        self._consistency = cost_consistency or {}
        # Per-resource cost-basis metadata (label / is_estimate / variability) so a finding can disclose
        # whether its saving is grounded in a real last month, a representative figure, or a run-rate.
        self._cost_basis = cost_basis or {}
        self._currency = (currency or "USD").upper()
        self._snapshot = snapshot_iso or "the inventory snapshot"
        self._debug = debug
        self._reservation_basis = reservation_basis
        # Absolute ceiling: the subscription's total measured monthly spend. No single optimisation can
        # save more than the customer actually spends, so any finding above this is a sign the saving
        # wasn't grounded in real billing — we clamp it rather than ever show savings that exceed spend.
        # None (no spend measured) disables the guard. This is a last-resort safety net; the correct
        # per-resource grounding/cap above is the primary mechanism.
        self._measured_monthly_spend = (
            measured_monthly_spend if (measured_monthly_spend and measured_monthly_spend > 0) else None
        )
        # Observed billing span (days). When the subscription has no complete billing month, a saving
        # grounded on a run-rate of fewer than MIN_BILLING_DAYS_FOR_ANNUAL days can't be defensibly
        # annualised → such findings are downgraded to REVIEW ("insufficient billing history"). None
        # means a complete billing month exists (annualisation is fine).
        self._billing_span_days = billing_span_days

    # -- shared builder --------------------------------------------------------

    def _cost_basis_partial(self, resource_id: Optional[str]) -> bool:
        """True when a resource hasn't billed a full representative month yet.

        `billed_months` (from the 6-month history) counts months with any cost. A value of 1 means the
        resource only started billing in the most recent month — a brand-new, recently-migrated, or
        just-provisioned resource — so its "last month" cost is a partial fragment that badly
        under-represents its real run-rate. In that case a saving grounded in that cost would be far
        too low (e.g. AHB on a 24×7 VM whose subscription was migrated mid-month), so the caller should
        price at the full-month run-rate instead and flag it as an estimate.
        """
        cons = self._consistency.get((resource_id or "").lower())
        billed = cons.get("billed_months") if cons else None
        return billed is not None and billed < 2

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
        evidence_state: str = QUANTIFIED, reference_monthly_price: Optional[float] = None,
        savings_source: str = QUANTIFIED_SAVINGS,
    ) -> Dict:
        """Build one finding, stamping its FINANCIAL EVIDENCE STATE.

        `evidence_state` = QUANTIFIED (a defensible, countable saving) or REVIEW (a real signal we can't
        price for this customer — no savings amount, optional clearly-labelled reference list price). The
        caller passes REVIEW (with an optional `reference_monthly_price`) when it has no actual billed cost
        to ground a saving. A QUANTIFIED finding is additionally downgraded to REVIEW here when its saving
        rests on a run-rate over too short a billing span to annualise (insufficient billing history).
        """
        monthly = round(monthly or 0.0, 2)
        resource_id = resource.get("id")
        details = dict(extra_details or {})
        conditional = bool(details.get("conditional"))
        cb = self._cost_basis.get((resource_id or "").lower()) if resource_id else None

        # ── Insufficient billing history: a run-rate saving over fewer than MIN_BILLING_DAYS_FOR_ANNUAL
        # days is too little evidence to annualise into a confident figure → downgrade to REVIEW and keep
        # the observed monthly only as a reference. (Conditional/AHB carries its own partial-billing note.)
        if (evidence_state == QUANTIFIED and monthly > 0 and not conditional
                and self._billing_span_days is not None
                and self._billing_span_days < MIN_BILLING_DAYS_FOR_ANNUAL
                and cb and cb.get("cost_basis") == "run_rate"):
            evidence_state = REVIEW
            reference_monthly_price = reference_monthly_price or monthly
            details["insufficient_billing_history"] = True
            details["billing_span_days"] = self._billing_span_days

        # ── REVIEW: a legitimate optimisation signal we cannot price for THIS customer. It NEVER carries a
        # savings amount and NEVER counts toward any total; it may show a clearly-labelled reference price.
        if evidence_state == REVIEW:
            details["evidence_state"] = REVIEW
            details["financial_impact"] = "Not quantified"
            details["savings_source"] = UNQUANTIFIED
            if reference_monthly_price and reference_monthly_price > 0:
                details["reference_monthly_price"] = round(reference_monthly_price, 2)
                details.setdefault("reference_price_source", AUTHORITATIVE_RETAIL_PRICE)
            return {
                "category": category,
                "display_name": CATEGORY_DISPLAY.get(category, category),
                "resource_id": resource_id,
                "resource_name": resource.get("name"),
                "subscription_id": resource.get("subscriptionId"),
                "resource_group": resource.get("resourceGroup"),
                "resource_type": resource_type,
                "estimated_savings_monthly": 0.0,   # REVIEW: no savings figure, ever
                "estimated_savings_annual": 0.0,
                # Contribution to the non-overlapping Total Identified Savings — always 0 for REVIEW.
                "counted_savings_monthly": 0.0,
                "counted_savings_annual": 0.0,
                "severity": "low",
                "confidence": round(clamp01(base_confidence * 0.6), 2),
                "description": description,
                "recommendation": recommendation,
                "advisor_recommendation_id": self._advisor_id_for(resource_id),
                "validation_status": UNVALIDATED,
                "validation_variance_pct": None,
                "actual_monthly_cost": None,
                "evidence_state": REVIEW,
                "debug_reason": debug_reason if self._debug else None,
                "details": details,
            }

        # ── QUANTIFIED path ───────────────────────────────────────────────────────────────
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

        # ABSOLUTE GUARANTEE: never present a single-finding saving that exceeds the customer's total
        # measured spend. A defensible optimisation can't save more than the customer actually pays;
        # anything above that means the figure wasn't grounded in real billing (e.g. a list-price delta
        # on a partial-billing subscription). Clamp it and flag it rather than ever show savings > spend.
        spend_capped = False
        if self._measured_monthly_spend is not None and monthly > self._measured_monthly_spend:
            monthly = round(self._measured_monthly_spend, 2)
            spend_capped = True

        confidence = combine_confidence(base_confidence, validation, has_price)
        advisor_id = self._advisor_id_for(resource_id)
        # An Advisor rec corroborating our own detection raises confidence.
        if advisor_id and category not in ("advisor_cost", "ri_vm"):
            confidence = round(min(1.0, confidence + 0.05), 2)

        if validation is not None:
            details["validation_note"] = validation.note
        if capped:
            details["savings_capped_at_actual_cost"] = True
        if spend_capped:
            details["savings_capped_at_measured_spend"] = True
        # Disclose the cost basis (actual last month / representative / run-rate) for a resource-bearing
        # finding, so the UI can flag an estimate or a high-variability resource and show both the
        # historical average and the current run-rate when they materially diverge. Aggregate findings
        # (id=None, e.g. AHB) set their own basis flags from their constituent resources.
        if cb and monthly > 0:
            details.setdefault("cost_basis", cb.get("cost_basis"))
            details.setdefault("cost_is_estimate", cb.get("cost_is_estimate"))
            details.setdefault("cost_variability", cb.get("cost_variability"))
            if cb.get("cost_material_divergence"):
                details.setdefault("historical_monthly", cb.get("historical_monthly"))
                details.setdefault("current_run_rate", cb.get("current_run_rate"))
        # Stamp the evidence state + value source so a quantified saving is never confused with a
        # potential/conditional one. Conditional (AHB) savings are POTENTIAL — realised only if the
        # customer owns the licences — and are excluded from every total downstream.
        details["evidence_state"] = QUANTIFIED
        details["savings_source"] = POTENTIAL_SAVINGS if conditional else savings_source

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
            # Contribution to the non-overlapping Total Identified Savings. Defaults to the finding's own
            # value; the overlap resolver (resolve_overlaps) reduces it when an RI and a right-sizing/idle
            # recommendation address the same VM's compute so the same spend is never counted twice.
            "counted_savings_monthly": monthly,
            "counted_savings_annual": round(monthly * 12, 2),
            "severity": severity_from_savings(monthly, advisor_impact, self._currency),
            "confidence": confidence,
            "description": description,
            "recommendation": recommendation,
            "advisor_recommendation_id": advisor_id,
            "validation_status": validation.status if validation else None,
            "validation_variance_pct": validation.variance_pct if validation else None,
            "actual_monthly_cost": validation.actual_monthly_cost if validation else None,
            "evidence_state": QUANTIFIED,
            # TODO: remove or gate behind admin-only role before prod (debug scaffolding).
            "debug_reason": debug_reason if self._debug else None,
            "details": details,
        }

    def _grounded_or_review(
        self, category: str, resource: Dict, resource_type: str, *,
        actual: Optional[float], reference_price: Optional[float],
        base_confidence: float, description: str, recommendation: str,
        debug_reason: Optional[str] = None, extra_details: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """One finding for a flat-rate / orphan / always-on resource, honouring the no-fabrication rule:

          * actual billed cost known → QUANTIFIED, saving = the resource's ACTUAL billed cost;
          * no billed cost but a live retail price → REVIEW, the retail rate shown ONLY as a clearly-
            labelled reference (never a saving, never in any total) — this is the Bastion/orphan case;
          * neither → None (SUPPRESSED: nothing defensible to say).

        This is the single place the old "list price AS the saving" behaviour was removed: a retail/list
        price is reference pricing, not customer spend, so it can never become a quantified saving.

        Sponsored/credited subscriptions: when the billed cost is present but a negligible fraction of the
        list price (compute effectively free — Azure Sponsorship/credit or a currency-scale mismatch), the
        resource still has economic value but the customer's saving can't be quantified from a near-zero
        bill, so it too becomes REVIEW rather than a tiny, misleading number.
        """
        sponsored = (
            actual is not None and actual > 0 and reference_price is not None and reference_price > 0
            and actual < reference_price * SPONSORED_COST_FRACTION
        )
        if actual is not None and actual > 0 and not sponsored:
            return self._finding(
                category, resource, resource_type, actual,
                base_confidence=base_confidence, description=description, recommendation=recommendation,
                has_price=True, grounded=True, savings_source=ACTUAL_BILLED_COST,
                debug_reason=debug_reason, extra_details=extra_details)
        if reference_price is not None and reference_price > 0:
            details = dict(extra_details or {})
            if sponsored:
                details["sponsored_or_credited"] = True
            return self._finding(
                category, resource, resource_type, 0.0,
                base_confidence=base_confidence, description=description, recommendation=recommendation,
                evidence_state=REVIEW, reference_monthly_price=reference_price,
                debug_reason=debug_reason, extra_details=details)
        return None

    # -- unattached / orphaned (ARG-authoritative) -----------------------------

    async def detect_unattached_disks(self, disks: List[Dict]) -> List[Dict]:
        out = []
        for disk in disks:
            sku = disk.get("skuName") or "Standard_LRS"
            size = int(disk.get("diskSizeGB") or 0)
            region = disk.get("location") or "eastus"
            rid = (disk.get("id") or "").lower()
            actual = self._cost_map.get(rid)
            try:
                price = await self._pricing.get_managed_disk_monthly_price(region, sku, size)
            except PricingUnavailableError:
                price = None
            reason = (
                f"disk unattached: diskState=='Unattached' in Resource Graph as of {self._snapshot}; "
                f"{size} GB {sku} in {region}; actual billed cost "
                f"{'unknown' if actual is None else f'{actual:.2f}'}, retail reference "
                f"{'n/a' if price is None else f'{price:.2f}'}/mo."
            )
            f = self._grounded_or_review(
                "unattached_managed_disks", disk, "microsoft.compute/disks",
                actual=actual, reference_price=price, base_confidence=0.9,
                description=(f"Managed disk '{disk.get('name')}' ({size} GB, {sku}) is unattached "
                            "and accruing storage cost with no VM using it."),
                recommendation="Delete the disk if unneeded, or re-attach it to a VM.",
                debug_reason=reason, extra_details=disk)
            if f:
                out.append(f)
        return out

    async def detect_orphaned_public_ips(self, ips: List[Dict]) -> List[Dict]:
        out = []
        for pip in ips:
            sku = pip.get("skuName") or "Basic"
            region = pip.get("location") or "eastus"
            rid = (pip.get("id") or "").lower()
            actual = self._cost_map.get(rid)
            try:
                price = await self._pricing.get_public_ip_monthly_price(region, sku)
            except PricingUnavailableError:
                price = None
            reason = (
                f"public IP orphaned: no ipConfiguration/natGateway in Resource Graph as of "
                f"{self._snapshot}; {sku} IP in {region}; actual billed cost "
                f"{'unknown' if actual is None else f'{actual:.2f}'}, retail reference "
                f"{'n/a' if price is None else f'{price:.2f}'}/mo."
            )
            f = self._grounded_or_review(
                "orphaned_public_ips", pip, "microsoft.network/publicipaddresses",
                actual=actual, reference_price=price, base_confidence=0.9,
                description=(f"Public IP '{pip.get('name')}' ({sku}) is not associated with any "
                            "resource and is billing idle."),
                recommendation="Delete the Public IP if it is no longer needed.",
                debug_reason=reason, extra_details=pip)
            if f:
                out.append(f)
        return out

    async def detect_idle_app_service_plans(self, plans: List[Dict]) -> List[Dict]:
        out = []
        for plan in plans:
            sku = plan.get("skuName") or ""
            region = plan.get("location") or "eastus"
            price = await self._pricing.get_app_service_plan_monthly_price(region, sku)
            actual = self._cost_map.get((plan.get("id") or "").lower())
            reason = (
                f"App Service Plan idle: numberOfSites==0 in Resource Graph as of {self._snapshot}; "
                f"SKU {sku or 'unknown'}; actual billed cost "
                f"{'unknown' if actual is None else f'{actual:.2f}'}, retail reference "
                f"{'n/a' if price is None else f'{price:.2f}'}/mo ({self._currency})."
            )
            # QUANTIFIED from the plan's ACTUAL billed cost; else REVIEW with the retail rate as reference.
            f = self._grounded_or_review(
                "idle_app_service_plans", plan, "microsoft.web/serverfarms",
                actual=actual, reference_price=price, base_confidence=0.75,
                description=(f"App Service Plan '{plan.get('name')}' ({plan.get('skuName')}) hosts "
                            "no apps and is incurring idle compute charges."),
                recommendation="Delete the plan or deploy apps to it.",
                debug_reason=reason, extra_details=plan)
            if f:
                out.append(f)
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
                recommendation=f"Scale the plan down from {sku} to {target_sku}." + _RIGHTSIZE_METHODOLOGY,
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
                recommendation=(f"Scale down from {current_vcores} to {target} vCores after confirming "
                                f"peak workloads.") + _RIGHTSIZE_METHODOLOGY,
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
                recommendation=(f"Scale down from {current_vcores} to {target} vCores after confirming "
                                f"peak workloads.") + _RIGHTSIZE_METHODOLOGY,
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
                                f"{cur_sku} → StandardSSD_LRS at the same size. Verify the workload first.")
                                + _DISK_METHODOLOGY,
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
        """Paused/inactive SQL DBs still bill for storage. The saving from removing one is its ACTUAL
        Cost Management billed cost — never a guess. Without per-resource cost data the saving can't be
        quantified, so it stays 0 and the pipeline drops it (no fabricated $0 opportunity)."""
        out = []
        for db in dbs:
            actual = self._cost_map.get((db.get("id") or "").lower())
            monthly = actual if (actual is not None and actual > 0) else 0.0
            reason = (
                f"SQL DB inactive: status=='{db.get('status')}' in Resource Graph as of "
                f"{self._snapshot}; actual billed cost {monthly:.2f}/mo (Cost Management)."
            )
            out.append(self._finding(
                "paused_sql_databases", db, "microsoft.sql/servers/databases", monthly,
                base_confidence=0.85,
                description=(f"SQL Database '{db.get('name')}' is '{db.get('status')}'. Storage "
                            "cost continues to accrue."),
                recommendation="Delete or archive the database if no longer required.",
                has_price=monthly > 0, debug_reason=reason,
                grounded=monthly > 0, actual_cost_override=monthly if monthly > 0 else None,
                extra_details=db,
            ))
        return out

    def detect_stopped_sql_managed_instances(self, mis: List[Dict]) -> List[Dict]:
        """Stopped SQL MIs keep billing vCores. Saving = the MI's ACTUAL Cost Management billed cost;
        unavailable → 0 (dropped by the pipeline) rather than a fabricated figure."""
        out = []
        for mi in mis:
            actual = self._cost_map.get((mi.get("id") or "").lower())
            monthly = actual if (actual is not None and actual > 0) else 0.0
            reason = (
                f"SQL MI stopped: state=='{mi.get('state')}' in Resource Graph as of {self._snapshot}; "
                f"actual billed cost {monthly:.2f}/mo (Cost Management)."
            )
            out.append(self._finding(
                "stopped_sql_managed_instances", mi, "microsoft.sql/managedinstances", monthly,
                base_confidence=0.9,
                description=(f"SQL Managed Instance '{mi.get('name')}' is '{mi.get('state')}'. MI is "
                            "billed for vCores continuously."),
                recommendation="Delete the Managed Instance if it is no longer needed.",
                has_price=monthly > 0, debug_reason=reason,
                grounded=monthly > 0, actual_cost_override=monthly if monthly > 0 else None,
                extra_details=mi,
            ))
        return out

    # -- rule-driven orphan/waste (ARG-authoritative, broad coverage) ---------

    async def detect_orphans(self, bucket: str, rows: List[Dict]) -> List[Dict]:
        """Evaluate a rule-driven orphan bucket (snapshots, empty LBs, NAT gw, Bastion).

        Pricing hierarchy — never a hardcoded estimate:
          * snapshots            → the snapshot's ACTUAL Cost Management billed cost (grounded);
          * Load Balancer / NAT / Bastion → the LIVE Retail Prices API rate, or the resource's
            actual billed cost when retail has no price.
        A row with no authoritative price is SKIPPED (a fabricated saving is never emitted).
        """
        rule = ORPHAN_RULES.get(bucket)
        if rule is None:
            return []
        out = []
        for row in rows:
            region = row.get("location") or "eastus"
            rid = (row.get("id") or "").lower()
            actual = self._cost_map.get(rid)
            # Reference retail rate (flat-rate resources only). Snapshots bill on incremental USED
            # storage, which retail can't express, so they have NO reference price — grounded-cost-only.
            if bucket == "orphaned_snapshots":
                reference_price = None
            elif bucket == "empty_load_balancers":
                reference_price = await self._pricing.get_load_balancer_monthly_price(region)
            elif bucket == "idle_nat_gateways":
                reference_price = await self._pricing.get_nat_gateway_monthly_price(region)
            elif bucket == "bastion_hosts":
                reference_price = await self._pricing.get_bastion_monthly_price(region, row.get("skuName") or "Basic")
            else:
                reference_price = None
            reason = (
                f"{CATEGORY_DISPLAY.get(rule.category, rule.category)}: matched by Resource Graph "
                f"state as of {self._snapshot}; actual billed cost "
                f"{'unknown' if actual is None else f'{actual:.2f}'}, retail reference "
                f"{'n/a' if reference_price is None else f'{reference_price:.2f}'}/mo ({self._currency})."
            )
            # QUANTIFIED only from the resource's ACTUAL billed cost. With no billed cost, the retail rate
            # is shown as a REVIEW reference (never a saving) — this is the Bastion/NAT/LB fix.
            f = self._grounded_or_review(
                rule.category, row, rule.resource_type,
                actual=actual, reference_price=reference_price, base_confidence=rule.base_confidence,
                description=rule.describe(row), recommendation=rule.recommendation,
                debug_reason=reason, extra_details=row)
            if f:
                out.append(f)
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
        metrics_unavailable: List[Dict] = []   # VMs discovered but whose metrics FAILED (≠ genuinely empty)
        for vm in vms:
            max_cpu = vm.get("max_cpu")
            avg_cpu = vm.get("avg_cpu")
            cpu_datapoints = int(vm.get("cpu_datapoints") or 0)
            peak_mem = vm.get("peak_memory_used_pct")
            memory_available = bool(vm.get("memory_available"))
            window = int(vm.get("metric_window_days") or METRIC_WINDOW_DAYS)
            if max_cpu is None or cpu_datapoints == 0:
                # No CPU metrics → cannot classify utilisation. Distinguish the two reasons:
                #  * metrics FAILED to retrieve (throttle/error) → the VM is un-assessable; surface it as
                #    REVIEW so the evidence model records "discovered but metrics unavailable" — NEVER idle.
                #  * Azure genuinely returned no datapoints → leave it out (legitimate "no data").
                if vm.get("metrics_failed"):
                    metrics_unavailable.append(vm)
                continue
            # GROUNDED-ONLY. An idle/resize saving MUST be grounded in the VM's ACTUAL billed cost so it
            # can never exceed what the customer really pays. Without per-resource billing a raw list-
            # price delta can dwarf actual spend (a D16s_v3's list price is ~₹47K/mo even if the VM has
            # billed almost nothing yet on a new/partial subscription) — that produced the "₹318K saving
            # against ₹34K spend" case. So a VM with no measured billed cost is skipped, not list-priced.
            vm_actual = self._cost_map.get((vm.get("id") or "").lower())
            if not vm_actual or vm_actual <= 0:
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
                    recommendation="Deallocate or delete this VM if it is no longer needed." + _IDLE_METHODOLOGY,
                    has_price=payg is not None, debug_reason=reason,
                    extra_details={
                        "avg_cpu": avg_cpu, "max_cpu": max_cpu, "peak_memory_used_pct": peak_mem,
                        "memory_verified": memory_available, "cpu_datapoints": cpu_datapoints,
                        # SKU/region so the overlap resolver can match this VM to an RI recommendation.
                        "vm_sku": sku, "vm_region": region,
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
                recommendation=f"Resize from {sku} to {target.sku}." + _RIGHTSIZE_METHODOLOGY,
                has_price=True, debug_reason=reason,
                extra_details={
                    "avg_cpu": avg_cpu, "max_cpu": max_cpu, "peak_memory_used_pct": peak_mem,
                    "memory_verified": memory_available, "cpu_datapoints": cpu_datapoints,
                    "current_sku": sku, "recommended_sku": target.sku,
                    # SKU/region so the overlap resolver can match this VM to an RI recommendation.
                    "vm_sku": sku, "vm_region": region,
                    "current_monthly_price": payg, "recommended_monthly_price": target_price,
                    # Absolute specs power the before/after visual on the frontend.
                    "current_vcpu": current_spec.vcpu if current_spec else None,
                    "current_memory_gb": current_spec.memory_gb if current_spec else None,
                    "recommended_vcpu": target.vcpu,
                    "recommended_memory_gb": target.memory_gb,
                    "downsize_ceiling_pct": DOWNSIZE_HEADROOM_CEILING,
                },
            ))
        # VMs discovered but un-assessable because their metrics FAILED to retrieve → ONE aggregated
        # REVIEW finding, so the evidence model explicitly records "discovered, metrics unavailable"
        # (never idle/right-sized from missing metrics) without a noisy per-VM card each.
        if metrics_unavailable:
            out.append(self._metrics_unavailable_review(metrics_unavailable))
        return out

    def _metrics_unavailable_review(self, vms: List[Dict]) -> Dict:
        """One REVIEW finding for VMs whose utilisation metrics could not be collected this run.

        Utilisation-dependent savings (idle / right-sizing) can't be quantified without metrics, so this
        is REVIEW / "Not quantified" — it carries the reason and the affected VMs, integrates with the
        Batch-1 evidence model (0 saving, excluded from every total), and is never a fabricated finding.
        """
        n = len(vms)
        affected = [{"name": vm.get("name"), "sku": vm.get("vmSize"), "region": vm.get("location")}
                    for vm in vms[:50]]
        sub_id = next((vm.get("subscriptionId") for vm in vms if vm.get("subscriptionId")), None)
        synthetic = {
            "id": None, "name": f"{n} VM{'s' if n != 1 else ''} — utilisation metrics unavailable",
            "subscriptionId": sub_id, "resourceGroup": None,
        }
        return self._finding(
            "vm_metrics_unavailable", synthetic, "microsoft.compute/virtualmachines", 0.0,
            base_confidence=0.5,
            description=(
                f"{n} running VM{'s were' if n != 1 else ' was'} discovered, but Azure Monitor "
                "utilisation metrics could not be collected for them this run (throttled or errored after "
                "retries). Idle / right-sizing savings can't be assessed without metrics, so they are not "
                "quantified — a failed metric is never treated as 0% utilisation."),
            recommendation=("Re-run the assessment once Azure Monitor metrics are available to evaluate "
                            "these VMs for idle / right-sizing savings."),
            evidence_state=REVIEW,
            extra_details={"metrics_unavailable": True, "affected_count": n, "affected_vms": affected,
                           "reason": "metrics_unavailable"},
        )

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
            },
        )

    # NOTE: `detect_vm_commitments` (the old retail-estimate VM RI detector) was REMOVED in the
    # accuracy audit. The Azure Retail Prices API does not publish reservation prices for most VM SKUs
    # (e.g. Dsv3), so that path either fabricated a discount or, once fabrication was removed, could not
    # price the most common VM series at all. VM Reserved Instances now come exclusively from Azure's
    # own reservation-recommendations engine via `commitments_from_recommendations` — the authoritative,
    # usage-based source (real savings, real eligibility, only reservable SKUs the customer actually
    # uses). See `commitments_from_recommendations` below and `reservations.py`.

    # -- commitments from Azure's own reservation engine (authoritative) -------

    def commitments_from_recommendations(self, groups: List[Dict]) -> List[Dict]:
        """Build reservation findings from parsed Consumption `reservationRecommendations`.

        This is the AUTHORITATIVE and ONLY source of Reserved Instance recommendations — for VMs and
        for every non-VM type (SQL / Cosmos / App Service / Files / Disks) alike. Azure's engine
        simulates the customer's real hourly usage over the look-back window at their real (negotiated)
        prices, excludes reservations already owned, and returns the SKU, quantity, term, on-demand
        cost, reserved cost and net savings that MAXIMISE savings. We never compute a discount, never
        assume a rate, and never recommend a reservation Azure didn't — so a VM only earns an RI when
        Microsoft's own engine says the customer's usage justifies one.

        A group with no positive saving on either term is dropped (nothing to recommend). Savings-plan
        recommendations are intentionally excluded — this tool recommends Reserved Instances only.
        Grouped into ONE finding per finding-category. Resource-less (escapes per-resource dedupe).
        """
        by_category: Dict[str, List[Dict]] = {}
        for g in groups:
            if g.get("category") == "savings_plan_vm":
                continue  # we recommend Reserved Instances, not Savings Plans
            terms = g.get("terms", {})
            p1 = terms.get("P1Y")
            p3 = terms.get("P3Y")
            head = p1 or p3
            # Validate we have enough authoritative data: a real recommendation with a positive net
            # saving. No recommendation / no saving → not surfaced (never a fabricated figure).
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
            unit = "VM" if category == "ri_vm" else "SKU"
            f = self._aggregate_commitment_finding(
                category, "Reserved Instance", items,
                source="azure_reservation_recommendations", base_confidence=0.9, unit=unit,
                grounded=True)  # Azure computes these on real usage at real prices — authoritative
            if f:
                out.append(f)
        return out

    # -- Windows Azure Hybrid Benefit (one classified finding) -----------------

    async def detect_windows_ahb(
        self, vms: List[Dict], exclude_ids: Optional[set] = None,
    ) -> List[Dict]:
        """One CONDITIONAL Azure Hybrid Benefit finding, grounded in each VM's ACTUAL eligible
        Windows-licence component.

        AHB is NOT free money. It lets a customer who ALREADY OWNS eligible Windows Server licences
        (with active Software Assurance, or qualifying subscription licences) drop the Windows licence
        premium baked into a Windows VM's price. So two hard rules:

          1. GROUND THE SAVING IN THE ACTUAL BILL. Per VM the saving is
             `actual billed cost × licence_fraction`, where `licence_fraction = (Windows list price −
             Linux list price) / Windows list price` from LIVE retail pricing for that exact SKU/region.
             This is the licence share of what the customer is REALLY billed — never a theoretical list
             price. It is additionally capped at the SKU's full list licence premium so it can never
             exceed the licence itself, and can never exceed the VM's own cost.

          2. NEVER FABRICATE. A VM is EXCLUDED (contributes nothing, counted separately) when we can't
             establish the licence premium (no live Windows/Linux price for the SKU) OR can't ground it
             (no measured billed cost — e.g. a brand-new/partial subscription resource that hasn't billed,
             or a deallocated box). We do NOT price the licence at list on a VM that isn't billing it, and
             we do NOT extrapolate a full-month/annual figure from insufficient billing to justify a saving.

        The whole finding is flagged `conditional` / `requires_license_ownership`: the customer only
        realises this if they own the licences, so the UI presents it as a POTENTIAL saving behind that
        prerequisite. Resource-less (`id=None`) so it escapes per-resource dedupe (AHB saves the licence,
        additive with any RI/downsize on the same VM's compute). `exclude_ids` drops VMs already
        recommended for deletion (idle/stopped) so their licence isn't double-counted.
        """
        exclude_ids = exclude_ids or set()
        eligible: List[Dict] = []
        excluded_no_pricing = 0   # Windows VM with no live licence premium for its SKU → can't quantify
        excluded_no_billing = 0   # Windows VM with no measured billed cost → can't ground → don't guess
        total = 0.0
        sub_id: Optional[str] = None
        partial_billing = False   # ≥1 eligible VM grounded on a partial (sub-month) billing window
        any_estimate = False      # ≥1 eligible VM grounded on a run-rate/representative (estimate) basis
        any_high_var = False      # ≥1 eligible VM sits on a high-variability (bursty) resource
        anomalous_low = 0         # eligible VMs whose billed cost is NEGLIGIBLE vs list (compute ≈ free)
        low_util = 0              # eligible VMs that simply run part-time (low, but plausible, uptime)
        for vm in vms:
            rid_l = (vm.get("id") or "").lower()
            if rid_l in exclude_ids:
                continue  # VM is being deleted (idle/stopped) → no licence to save
            sku = vm.get("vmSize") or ""
            region = vm.get("location") or "eastus"
            try:
                windows = await self._pricing.get_vm_windows_monthly_price(region, sku)
                # Compute-only (post-AHB) rate = a same-size Linux VM: identical hardware, no licence.
                compute_only = await self._pricing.get_vm_monthly_price(region, sku)
            except PricingUnavailableError:
                windows = compute_only = None
            # (1) Authoritative licence premium for THIS exact SKU/region — or exclude (no guessing).
            # The per-vCore Windows Server charge is NOT uniform across families (a B-series burstable VM
            # carries a far lower licence per vCore than a D/E-series one), so each VM uses its OWN delta.
            if not (windows and compute_only and windows > compute_only):
                excluded_no_pricing += 1
                continue
            licence_premium = round(windows - compute_only, 2)   # full list licence for this SKU (ref)
            licence_fraction = licence_premium / windows
            # (2) Ground in the VM's ACTUAL billed cost. No billing → we cannot establish a realisable
            # licence saving, so EXCLUDE rather than price the licence at list (that was the ₹888K bug).
            actual = self._cost_map.get(rid_l)
            if actual is None or actual <= 0:
                excluded_no_billing += 1
                continue
            # Effective uptime = the fraction of the month this VM billed at its licence-free (Linux) rate.
            # LOW uptime is a part-time/lab VM (legitimate — the grounded saving scales with it, no alarm).
            # A NEGLIGIBLE ratio means compute is effectively free → sponsored/credited sub or a
            # billing-scale/currency mismatch, which we DO flag. This distinction is what stops every
            # mostly-deallocated VM from tripping a false "verify billing" warning.
            uptime = actual / compute_only if compute_only > 0 else 0.0
            if uptime < ANOMALOUS_UPTIME_FRACTION:
                anomalous_low += 1
            elif uptime < LOW_UTILISATION_FRACTION:
                low_util += 1
            # Saving = the licence share of what they ACTUALLY pay, capped at the list licence premium so
            # it can never exceed the licence itself (nor, since fraction ≤ 1, the VM's own bill).
            monthly = round(min(actual * licence_fraction, licence_premium), 2)
            if monthly <= 0:
                excluded_no_billing += 1
                continue
            partial_billing = partial_billing or self._cost_basis_partial(vm.get("id"))
            cb = self._cost_basis.get(rid_l, {})
            any_estimate = any_estimate or bool(cb.get("cost_is_estimate"))
            any_high_var = any_high_var or (cb.get("cost_variability") == "high")
            sub_id = sub_id or vm.get("subscriptionId")
            eligible.append({
                "name": vm.get("name"), "sku": sku, "region": region,
                "monthly_savings": monthly, "resource_id": vm.get("id"),
                "actual_cost_based": True, "cost_basis": cb.get("cost_basis"),
                # Audit trail — verifiable inputs. licence_charge = this VM's own (Windows − Linux) retail
                # delta; saving = min(actual × fraction, licence_charge).
                "windows_price": round(windows, 2), "compute_only_price": round(compute_only, 2),
                "licence_charge": licence_premium, "licence_fraction": round(licence_fraction, 4),
                "vcpu": _vcpus(sku), "actual_monthly_cost": round(actual, 2),
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
        excluded = excluded_no_pricing + excluded_no_billing
        synthetic = {
            "id": None, "name": f"{n} Windows VM{'s' if n != 1 else ''} eligible for AHB",
            "subscriptionId": sub_id, "resourceGroup": None,
        }
        partial_note = (
            " These figures reflect the licence share of the spend billed SO FAR — this subscription has "
            "less than one complete billing month (recently created or migrated), so re-run after a full "
            "billing month for a complete picture." if partial_billing else ""
        )
        excl_note = ""
        if excluded:
            parts = []
            if excluded_no_billing:
                parts.append(f"{excluded_no_billing} with no measured billing")
            if excluded_no_pricing:
                parts.append(f"{excluded_no_pricing} with no live licence price")
            excl_note = (f" A further {excluded} Windows VM{'s' if excluded != 1 else ''} "
                         f"({', '.join(parts)}) could not be quantified and are excluded from the total.")
        # Anomaly: most eligible VMs bill NEGLIGIBLY vs their size's list price (compute effectively free)
        # → the data is suspect. Flag loudly. This is now distinct from ordinary low utilisation.
        cost_anomaly = anomalous_low > 0 and anomalous_low >= (n + 1) // 2
        anomaly_note = (
            f" ⚠ Billed cost for {anomalous_low} of {n} VMs is far below their size's list price — compute "
            "is effectively free, which usually means a sponsored/credited/trial subscription OR a "
            "billing-data/currency issue. Verify your subscription type and billing currency before relying "
            "on these figures." if cost_anomaly else ""
        )
        # Not an anomaly, but most VMs run part-time → the licence saving is small simply because the VMs
        # are billed for only part of the month. Explain that plainly instead of raising a false alarm.
        low_utilisation = not cost_anomaly and (anomalous_low + low_util) >= (n + 1) // 2
        low_util_note = (
            " Most of these VMs run intermittently (billed for only part of the month), so the licence "
            "saving reflects their limited runtime — it scales up as they run more." if low_utilisation else ""
        )
        reason = (
            f"{n} Windows VMs grounded in actual billed cost × per-VM (Windows − Linux) licence fraction "
            f"(capped at the SKU's list licence premium); total ${total:.2f}/mo across: {shown}. "
            f"Excluded: {excluded_no_billing} no-billing, {excluded_no_pricing} no-price." + partial_note
        )
        return [self._finding(
            "windows_ahb", synthetic, "microsoft.compute/virtualmachines", total,
            base_confidence=0.7,
            description=(
                f"POTENTIAL saving — conditional on licence ownership. {n} Windows "
                f"VM{'s' if n != 1 else ''} currently pay Azure's Windows Server licence on top of compute "
                f"({shown}). IF you already own eligible Windows Server licences with active Software "
                "Assurance (or qualifying subscription licences), Azure Hybrid Benefit removes that licence "
                "charge. The amount shown is the Windows licence share of these VMs' ACTUAL billed cost — "
                "not a theoretical list price, and not automatic: you only realise it on VMs your licences "
                f"cover.{partial_note}{excl_note}{anomaly_note}{low_util_note}"
            ),
            recommendation=(
                "Apply Azure Hybrid Benefit (licenseType=Windows_Server) ONLY to the VMs covered by "
                "eligible Windows Server licences you already own; VMs without eligible licences must stay "
                "on their current pay-as-you-go Windows licensing. You do not receive this saving simply "
                "by enabling AHB — it depends on owning the licences (with active Software Assurance or a "
                "qualifying subscription)."
            ),
            has_price=True, debug_reason=reason,
            grounded=True,  # every eligible VM is grounded in its actual billed cost
            extra_details={
                "eligible_vms": eligible, "eligible_count": n, "licence_kind": "Windows Server",
                # Eligibility is CONDITIONAL on licence ownership — the UI must present this as potential.
                "conditional": True, "requires_license_ownership": True,
                "excluded_count": excluded, "excluded_no_billing": excluded_no_billing,
                "excluded_no_pricing": excluded_no_pricing, "partial_billing": partial_billing,
                "savings_label": "Potential annual savings",
                # Cost-basis disclosure: True when any VM's saving rests on a run-rate/representative
                # (estimated) figure rather than a completed month's actual bill.
                "cost_is_estimate": any_estimate or partial_billing,
                "cost_variability": "high" if any_high_var else "low",
                # Anomaly: most VMs bill NEGLIGIBLY vs list (compute ≈ free) → figures untrustworthy.
                "cost_anomaly": cost_anomaly, "anomalous_low_count": anomalous_low,
                # Low utilisation: most VMs simply run part-time — a soft, non-alarming explanation.
                "low_utilization": low_utilisation,
            },
        )]

    # -- SQL Server Azure Hybrid Benefit (one classified finding) ---------------

    async def detect_sql_ahb(
        self, sql_resources: List[Dict], exclude_ids: Optional[set] = None,
    ) -> List[Dict]:
        """RETIRED — SQL Server Azure Hybrid Benefit is no longer quantified, because the SQL licence
        component cannot be reliably derived from any authoritative Microsoft pricing API.

        Why: the Azure Retail Prices API exposes only ONE consumption price per SQL vCore (the
        licence-included rate) — there is no separate "Azure Hybrid Benefit / base compute" meter, so
        the licence portion isn't published anywhere we can read live. The previous implementation used
        a hardcoded $112/vCore/month constant which, verified against the live API, is ≈ the ENTIRE
        GP Gen5 per-vCore compute price (~$111/vCore) — i.e. it claimed AHB removes essentially the whole
        compute cost, grossly over-stating the saving. Under this tool's rule ("if the required pricing
        cannot be reliably derived, do not fabricate the saving"), we surface no SQL AHB dollar figure
        rather than a wrong one. Detection of AHB-eligible SQL still exists in the KQL layer; if Microsoft
        later exposes the base-compute/licence split via an API, re-implement here from that live source.
        """
        return []

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
            problem = (short.get("problem") or "").strip()
            solution = (short.get("solution") or "").strip()
            f = self._finding(
                "advisor_cost", resource, props.get("impactedField", ""), monthly,
                base_confidence=0.85,  # Azure-computed
                description=problem or solution or "Azure Advisor cost recommendation.",
                recommendation=solution or "Follow the Azure Advisor recommendation.",
                has_price=True, advisor_impact=impact, debug_reason=reason,
                extra_details={"impact": impact, "extended_properties": ext},
            )
            # Show the SPECIFIC recommendation (e.g. "Right-size or shut down underutilized VMs") as the
            # title, not a generic "Azure Advisor Cost Recommendation" — a vague passthrough is useless
            # to a client. The Advisor source is conveyed by the small "Advisor" tag in the UI.
            if problem:
                f["display_name"] = problem
            f["advisor_recommendation_id"] = rec.get("id")  # Advisor rec is itself the correlation id
            out.append(f)
        return out
