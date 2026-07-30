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
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from .cost_management import UNVALIDATED, VALIDATED, ValidationResult, validate_savings
from .currency import from_usd, to_usd
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

SEV_ORDER = ["low", "medium", "high", "critical"]

CATEGORY_DISPLAY = {
    "unattached_managed_disks": "Unattached Managed Disks",
    "orphaned_public_ips": "Orphaned Public IP Addresses",
    "idle_app_service_plans": "Idle App Service Plans",
    "deallocated_vms": "Deallocated Virtual Machines",
    "paused_sql_databases": "Paused/Inactive SQL Databases",
    "stopped_sql_managed_instances": "Stopped SQL Managed Instances",
    "idle_vms": "Idle Virtual Machines",
    "oversized_vms": "Oversized Virtual Machines",
    "ri_vm": "Reserved Instance (VM)",
    "savings_plan_vm": "Savings Plan (VM)",
    "vm_rightsizing": "VM Rightsizing",
    "windows_ahb": "Windows Azure Hybrid Benefit",
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
# Fixed monthly estimates for resources whose cost isn't easily retail-priced (config-dependent);
# grounded by the actual-cost cap + validation when Cost Management data is available. Hygiene-only
# resources (NICs/NSGs/route tables) carry $0 — reported for completeness, not savings.
_SNAPSHOT_PER_GB = 0.05  # standard incremental snapshot ~$0.05/GB/mo


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
        lambda r: round(int(r.get("diskSizeGB") or 0) * _SNAPSHOT_PER_GB, 2),
        0.85,
    ),
    "empty_load_balancers": OrphanRule(
        "empty_load_balancers", "microsoft.network/loadbalancers",
        "Delete the load balancer if it is not routing traffic.",
        lambda r: (f"Standard Load Balancer '{r.get('name')}' has no backend pool — it bills "
                   "without balancing anything."),
        lambda r: 18.0, 0.85,
    ),
    "idle_nat_gateways": OrphanRule(
        "idle_nat_gateways", "microsoft.network/natgateways",
        "Delete the NAT gateway if no subnet uses it.",
        lambda r: (f"NAT Gateway '{r.get('name')}' is not associated with any subnet, yet bills a "
                   "fixed hourly rate."),
        lambda r: 32.0, 0.85,
    ),
    "bastion_hosts": OrphanRule(
        "bastion_hosts", "microsoft.network/bastionhosts",
        "Confirm Bastion is still needed, or deallocate it when not in use.",
        lambda r: (f"Azure Bastion '{r.get('name')}' ({r.get('skuName') or 'Standard'}) is a "
                   "fixed-cost resource (~$138/mo) — verify it is still required."),
        lambda r: 138.0, 0.6,
    ),
    "geo_redundant_vaults": OrphanRule(
        "backup_redundancy", "microsoft.recoveryservices/vaults",
        "If cross-region recovery isn't required, switch backup storage from Geo- to Locally-"
        "redundant (LRS) to roughly halve backup storage cost.",
        lambda r: (f"Recovery Services vault '{r.get('name')}' uses geo-redundant (GRS) backup "
                   "storage. If a geo-secondary copy isn't required, LRS costs about half."),
        # Nominal estimate — actual depends on backup volume (not available from Resource Graph).
        lambda r: 25.0, 0.4,
    ),
}

# App Service Plan monthly estimate (USD) — retail-priced ASP is deferred (see memory.md).
_ASP_COST = {
    "f1": 0, "d1": 9.49, "b1": 13.14, "b2": 26.28, "b3": 52.56,
    "s1": 56.94, "s2": 113.88, "s3": 227.76,
    "p1v2": 73.0, "p2v2": 146.0, "p3v2": 292.0,
    "p0v3": 37.23, "p1v3": 75.0, "p2v3": 150.0, "p3v3": 300.0,
    "i1v2": 294.0, "i2v2": 588.0, "i3v2": 1176.0,
}


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
        grounded: bool = False,
    ) -> Dict:
        monthly = round(monthly or 0.0, 2)
        resource_id = resource.get("id")
        # Validation compares the *original* estimate to actual cost (records the overage).
        validation = validate_savings(monthly, resource_id, self._cost_map) if monthly > 0 else None
        # A saving DERIVED from the resource's actual billed cost (AHB = licence fraction of real cost;
        # commitment = discount % of real cost) is inherently validated even when it's an aggregate
        # with no single resource id to match. Mark it so — otherwise it mislabels as an unvalidated
        # "list-price estimate", which is exactly backwards for the numbers that ARE grounded.
        if grounded and monthly > 0 and (validation is None or validation.status == UNVALIDATED):
            validation = ValidationResult(VALIDATED, None, None, "Grounded in the resource's actual billed cost.")
        confidence = combine_confidence(base_confidence, validation, has_price)
        advisor_id = self._advisor_id_for(resource_id)
        # An Advisor rec corroborating our own detection raises confidence.
        if advisor_id and category not in ("advisor_cost", "ri_vm", "savings_plan_vm"):
            confidence = round(min(1.0, confidence + 0.05), 2)

        details = dict(extra_details or {})
        if validation is not None:
            details["validation_note"] = validation.note

        # Cap: you can't save more on a resource than it actually costs. When we know the resource's
        # real billed cost, clamp the estimate to it so totals can never exceed measured spend.
        capped = False
        if validation is not None and validation.actual_monthly_cost is not None:
            actual = validation.actual_monthly_cost
            if actual >= 0 and monthly > actual:
                monthly = round(actual, 2)
                capped = True
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

    def detect_idle_app_service_plans(self, plans: List[Dict]) -> List[Dict]:
        out = []
        for plan in plans:
            sku = (plan.get("skuName") or "").lower()
            monthly = from_usd(_ASP_COST.get(sku, 0.0), self._currency)  # ASP table is USD
            reason = (
                f"App Service Plan idle: numberOfSites==0 in Resource Graph as of {self._snapshot}; "
                f"SKU {sku or 'unknown'} estimated {monthly:.2f}/mo ({self._currency})."
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

    def detect_deallocated_vms(self, vms: List[Dict]) -> List[Dict]:
        out = []
        for vm in vms:
            reason = (
                f"VM deallocated: powerState=='{vm.get('powerState')}' in Resource Graph as of "
                f"{self._snapshot}; compute stops but OS/data disks still bill."
            )
            out.append(self._finding(
                "deallocated_vms", vm, "microsoft.compute/virtualmachines", 0.0,
                base_confidence=0.9,
                description=(f"VM '{vm.get('name')}' ({vm.get('vmSize')}) is deallocated; its disks "
                            "continue to accrue storage cost."),
                recommendation="Delete the VM and its disks if unneeded, else disregard.",
                has_price=True, debug_reason=reason, extra_details=vm,
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

    def detect_orphans(self, bucket: str, rows: List[Dict]) -> List[Dict]:
        """Evaluate a rule-driven orphan bucket (snapshots, empty LBs, NAT gw, NICs, NSGs…)."""
        rule = ORPHAN_RULES.get(bucket)
        if rule is None:
            return []
        out = []
        for row in rows:
            monthly = from_usd(rule.monthly_cost(row), self._currency)  # rule estimates are USD
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
    ) -> Optional[Dict]:
        """Roll many per-SKU/VM commitment items into ONE finding.

        `kind` is "Reserved Instance" or "Savings Plan". Each item carries `s1` (1-year) and optional
        `s3` (3-year) monthly saving. The **best case (3-year, the deepest discount) is the counted
        headline**; the 1-year option is shown alongside so the client can pick the shorter commitment.
        Resource-less (`id=None`) so it isn't collapsed by the per-resource dedupe.
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
        one_note = (f" A shorter 1-year commitment saves about ${one_total:,.0f}/mo."
                    if has_3yr and one_total > 0 else "")
        reason = (
            f"Aggregated {n} {kind} candidate{plural} ({source}); best case 3-year "
            f"${three_total:.2f}/mo, 1-year ${one_total:.2f}/mo. {unit}s: {shown}."
        )
        return self._finding(
            category, synthetic, "microsoft.consumption/reservationrecommendations", headline,
            base_confidence=base_confidence,
            description=(
                f"{n} {unit}{plural} run steadily enough to commit ({shown}). A {kind} locks in a "
                f"lower rate than pay-as-you-go in exchange for a 1- or 3-year commitment. The best "
                f"case — a 3-year term — saves about ${three_total:,.0f}/mo "
                f"(${three_total * 12:,.0f}/yr).{one_note}"
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

    async def detect_vm_commitments(
        self, vms: List[Dict], windows_no_ahb_ids: Optional[set] = None,
    ) -> List[Dict]:
        """Retail-estimate fallback (used only when Azure's reservation engine returns nothing).

        A commitment saves a *fraction* of a VM's cost — the RI/Savings-Plan discount — so the saving
        is `base × discount_ratio`, NOT the whole cost. The base is the COMPUTE portion of the VM's
        actual cost (the Windows licence is stripped out, since AHB covers that separately and would
        otherwise be double-counted); when no billing data exists we fall back to the retail rate.
        RI-eligible SKUs roll into one "Reserved Instances" finding (1yr + 3yr); SKUs Azure only offers
        a Savings Plan for roll into one "Savings Plans" finding.
        """
        if self._reservation_basis == "advisor":
            return []
        windows_no_ahb_ids = windows_no_ahb_ids or set()
        ri_items: List[Dict] = []
        sp_items: List[Dict] = []
        ri_all_steady = True
        sp_all_steady = True
        cost_available = bool(self._cost_map)
        for vm in vms:
            max_cpu = vm.get("max_cpu")
            peak_mem = vm.get("peak_memory_used_pct")
            mem_ok = bool(vm.get("memory_available"))
            datapoints = int(vm.get("cpu_datapoints") or 0)
            window = int(vm.get("metric_window_days") or METRIC_WINDOW_DAYS)
            # Skip genuinely idle VMs — you delete those, not reserve them (idle detector covers).
            if (max_cpu is not None and max_cpu < IDLE_MAX_CPU
                    and mem_ok and peak_mem is not None and peak_mem < IDLE_MAX_MEMORY_PCT):
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
                sp1 = await self._pricing.get_vm_savings_plan_monthly_price(region, sku, "1 Year")
                sp3 = await self._pricing.get_vm_savings_plan_monthly_price(region, sku, "3 Years")
            except PricingUnavailableError:
                continue
            if not payg:
                continue

            # Discount RATIOS from retail (currency-independent); apply them to a real cost base.
            def _ratio(price):
                return (payg - price) / payg if price and payg and payg > price else None
            ri1_r, ri3_r = _ratio(ri1), _ratio(ri3)
            sp1_r, sp3_r = _ratio(sp1), _ratio(sp3)
            # Only recommend what Azure actually offers for this SKU/region — many D-series expose only
            # a Savings Plan, so fabricating an RI there is misleading.
            ri_available = ri1_r is not None or ri3_r is not None

            # Base = the COMPUTE portion of the VM's actual cost. For a Windows VM not yet on AHB, strip
            # the licence (compute_fraction = compute-only/Windows retail) so the commitment discount
            # applies to compute only and doesn't overlap AHB. Linux / already-AHB VMs → all compute.
            is_win_no_ahb = bool(rid and rid.lower() in windows_no_ahb_ids)
            compute_fraction = (payg / windows) if (is_win_no_ahb and windows and windows > payg) else 1.0
            if actual is not None and actual > 0:
                base = round(actual * compute_fraction, 2)
                ondemand = round(actual, 2)
            else:
                base = payg                     # no billing data → retail run-rate
                ondemand = payg

            r1, r3, reserved_price = (ri1_r, ri3_r, ri1) if ri_available else (sp1_r, sp3_r, sp1)
            s1 = round(base * r1, 2) if r1 else None
            s3 = round(base * r3, 2) if r3 else None

            # Steadiness: prefer real month-over-month billing history (the manual-report check) over
            # the metric-coverage proxy. A resource billed consistently across months is safe to
            # commit; one that swings is still surfaced (combined basis) but flagged + lower confidence.
            cons = self._consistency.get((rid or "").lower())
            if cons and cons.get("billed_months"):
                steady = bool(cons.get("stable"))
            else:
                steady = bool(window and datapoints >= 0.8 * window)
            if self._reservation_basis == "measured" and not steady:
                continue

            base_item = {
                "name": vm.get("name"), "sku": sku, "region": region, "quantity": 1,
                "ondemand": ondemand, "subscription_id": vm.get("subscriptionId"),
                "reserved": round(ondemand - (s1 or 0), 2),
                "billed_months": cons.get("billed_months") if cons else None,
                "cost_stable": cons.get("stable") if cons else None,
                # Audit trail: saving = compute_base × discount%. compute_base strips the Windows
                # licence for Windows VMs so it never overlaps AHB.
                "compute_base": round(base, 2), "actual_monthly_cost": round(actual, 2) if actual else None,
                "discount_1yr": round(r1, 4) if r1 else None,
                "discount_3yr": round(r3, 4) if r3 else None,
                "grounded": actual is not None and actual > 0,
            }
            if ri_available and (s1 is not None or s3 is not None):
                ri_items.append({**base_item, "s1": s1, "s3": s3})
                ri_all_steady = ri_all_steady and steady
            elif s1 is not None or s3 is not None:   # Savings Plan (RI not offered for this SKU)
                sp_items.append({**base_item, "s1": s1, "s3": s3})
                sp_all_steady = sp_all_steady and steady

        out: List[Dict] = []
        if ri_items:
            conf = 0.85 if ri_all_steady else 0.55
            f = self._aggregate_commitment_finding(
                "ri_vm", "Reserved Instance", ri_items,
                source="retail_estimate", base_confidence=conf, unit="VM", grounded=cost_available)
            if f:
                out.append(f)
        if sp_items:
            conf = 0.85 if sp_all_steady else 0.55
            f = self._aggregate_commitment_finding(
                "savings_plan_vm", "Savings Plan", sp_items,
                source="retail_estimate", base_confidence=conf, unit="VM", grounded=cost_available)
            if f:
                out.append(f)
        return out

    # -- commitments from Azure's own reservation engine (authoritative) -------

    def commitments_from_recommendations(self, groups: List[Dict]) -> List[Dict]:
        """Build reservation findings from parsed Consumption `reservationRecommendations`.

        These are SKU-level *purchase* recommendations (a reservation covers matching resources in a
        region, not one VM), computed by Azure on real usage at the customer's real prices, excluding
        reservations already owned — so they replace our retail-estimate `detect_vm_commitments` for
        any resource type Azure returns. Grouped into ONE finding per finding-category (e.g. all VM
        reservations together): 1-year headline total, 3-year total alongside, every SKU listed in
        `details.reservation_items`. Resource-less (escapes dedupe).
        """
        by_category: Dict[str, List[Dict]] = {}
        for g in groups:
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
        eligible: List[Dict] = []
        total = 0.0
        sub_id: Optional[str] = None
        cost_available = bool(self._cost_map)  # per-resource Cost Management data present?
        for vm in vms:
            if (vm.get("id") or "").lower() in exclude_ids:
                continue  # VM is being deleted (idle/stopped) → no licence to save
            sku = vm.get("vmSize") or ""
            region = vm.get("location") or "eastus"
            actual = self._cost_map.get((vm.get("id") or "").lower())
            # You can't save the Windows licence on a VM you aren't paying for. When we have
            # per-resource billing, skip VMs that show no measured cost in the window (deallocated /
            # demo boxes not actually billing) — otherwise AHB inflates on list price. Only when NO
            # per-resource billing is available do we fall back to the raw retail delta.
            if cost_available and (not actual or actual <= 0):
                continue
            try:
                windows = await self._pricing.get_vm_windows_monthly_price(region, sku)
                # Compute-only (post-AHB) rate = a same-size Linux VM: identical hardware, NO Windows
                # licence. It equals what the VM costs once AHB is applied, so the gap up to the
                # Windows price is exactly the licence charge. (Linux is only a measuring stick here.)
                compute_only = await self._pricing.get_vm_monthly_price(region, sku)
            except PricingUnavailableError:
                continue
            if not windows or not compute_only:
                continue
            # The licence is the ONLY difference between the Windows price and the compute-only price.
            # Take it as a fraction of the Windows price, then apply it to the VM's ACTUAL cost so the
            # saving tracks real, part-time spend instead of full-time list price.
            lic_fraction = max(0.0, (windows - compute_only) / windows) if windows else 0.0
            if cost_available:
                monthly = round(actual * lic_fraction, 2)
                grounded = True
            else:
                monthly = round(max(0.0, windows - compute_only), 2)
                grounded = False
            if monthly <= 0:
                continue
            sub_id = sub_id or vm.get("subscriptionId")
            eligible.append({
                "name": vm.get("name"), "sku": sku, "region": region,
                "monthly_savings": monthly, "resource_id": vm.get("id"),
                "actual_cost_based": grounded,
                # Audit trail — the exact inputs so the figure is verifiable against the calculator:
                # licence = windows − compute_only; saving = (grounded ? actual : windows) × fraction.
                "windows_price": round(windows, 2), "compute_only_price": round(compute_only, 2),
                "licence_charge": round(windows - compute_only, 2),
                "licence_fraction": round(lic_fraction, 4),
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
            "id": None, "name": f"{n} Windows VM{'s' if n != 1 else ''} eligible for AHB",
            "subscriptionId": sub_id, "resourceGroup": None,
        }
        grounded_count = sum(1 for e in eligible if e.get("actual_cost_based"))
        reason = (
            f"{n} running Windows VMs without licenseType=Windows_Server; licence fraction "
            f"(Windows − compute-only)/Windows applied to actual cost for {grounded_count}/{n} "
            f"(retail delta fallback for the rest); ${total:.2f}/mo across: {shown}."
        )
        return [self._finding(
            "windows_ahb", synthetic, "microsoft.compute/virtualmachines", total,
            base_confidence=0.7,
            description=(
                f"These {n} Windows VM{'s are' if n != 1 else ' is'} paying Azure's built-in Windows "
                f"Server licence charge on top of compute ({shown}). Azure Hybrid Benefit removes that "
                "charge — you pay only for compute — by applying a Windows Server licence with active "
                f"Software Assurance. The figure below is that licence portion of each VM's current "
                "bill (1 licence covers up to 16 cores). It is a REALISED saving only if you already "
                "own eligible licences. If you'd need to buy them, the licence is a separate cost — "
                "though because the Azure hourly charge is high, it typically pays back within a few "
                "months on an always-on VM; confirm your licensing position before counting it."
            ),
            recommendation=("Confirm the Windows Server licences (with Software Assurance) you already "
                            "own, then set 'Azure Hybrid Benefit' (licenseType=Windows_Server) on each "
                            "covered VM. Where you have no spare licence, leave the VM pay-as-you-go or "
                            "price a licence purchase against this hourly charge first."),
            has_price=True, debug_reason=reason,
            # Grounded when every eligible VM's saving came from its actual bill (not the retail delta).
            grounded=cost_available and grounded_count == n,
            extra_details={"eligible_vms": eligible, "eligible_count": n},
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
