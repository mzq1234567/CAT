"""Tests for the findings engine (Step 6)."""
from __future__ import annotations

from app.models.db import Finding
from app.services import assessment as pipeline
from app.services.currency import from_usd
from app.services.findings import (
    FindingsEngine,
    build_advisor_index,
    metrics_confidence,
    severity_from_savings,
)

DISK_RID = "/subscriptions/sub-1/resourceGroups/rg-a/providers/microsoft.compute/disks/disk-1"
VM_RID = "/subscriptions/sub-1/resourceGroups/rg-a/providers/microsoft.compute/virtualmachines/vm-1"

FINDING_COLUMNS = {c.name for c in Finding.__table__.columns} - {"id", "assessment_id"}


# Realistic same-series price ladder (roughly halves per step down, like real Dsv3 pricing).
DEFAULT_VM_PRICES = {
    "Standard_D16s_v3": 560.64, "Standard_D8s_v3": 280.32,
    "Standard_D4s_v3": 140.16, "Standard_D2s_v3": 70.08,
}


DEFAULT_RI = {  # (sku, term) -> monthly reserved price
    ("Standard_D16s_v3", "1 Year"): 400.0, ("Standard_D16s_v3", "3 Years"): 350.0,
    ("Standard_D2s_v3", "1 Year"): 50.0, ("Standard_D2s_v3", "3 Years"): 40.0,
}
DEFAULT_SP = {("Standard_D16s_v3", "1 Year"): 450.0, ("Standard_D2s_v3", "1 Year"): 55.0}
DEFAULT_WIN = {"Standard_D2s_v3": 137.24, "Standard_D16s_v3": 900.0}


# Live-retail prices the fake engine returns for flat-rate resources (USD; converted to the billing
# currency like the real engine). These stand in for the Retail Prices API — there are no hardcoded
# fallbacks in production any more, so the double returns a concrete live price or None.
_FAKE_FLAT_USD = {"lb": 18.0, "nat": 32.0, "bastion": 138.0}
_FAKE_ASP_USD = {  # per-SKU live App Service Plan prices used by the ASP tests
    "b1": 13.14, "b2": 26.28, "b3": 52.56, "s1": 56.94, "s2": 113.88, "s3": 227.76,
    "p1v2": 73.0, "p2v2": 146.0, "p3v2": 292.0, "p0v3": 37.23, "p1v3": 75.0, "p2v3": 150.0, "p3v3": 300.0,
}


class FakePricing:
    """Deterministic stand-in for PricingEngine (no network). VM price is per-SKU so the engine's
    'price current vs price target' downsize math can be tested against real deltas. Flat-rate and disk
    prices mimic the LIVE Retail Prices API (no hardcoded fallback); pass a value to `flat`/`asp`/`disk`
    (or set to return None) to simulate the API having no price for a resource."""
    def __init__(self, disk=19.71, pip=3.65, vm_prices=None, ri=None, sp=None, win=None, currency="USD",
                 flat=None, asp=None):
        self.disk = disk
        self.pip = pip
        self.vm_prices = dict(DEFAULT_VM_PRICES if vm_prices is None else vm_prices)
        self.ri = dict(DEFAULT_RI if ri is None else ri)
        self.sp = dict(DEFAULT_SP if sp is None else sp)
        self.win = dict(DEFAULT_WIN if win is None else win)
        self.currency = currency  # flat-rate methods return in this currency (like the real engine)
        self.flat = dict(_FAKE_FLAT_USD if flat is None else flat)
        self.asp = dict(_FAKE_ASP_USD if asp is None else asp)

    async def get_managed_disk_monthly_price(self, region, sku, size):
        return self.disk

    async def get_public_ip_monthly_price(self, region, sku="Standard"):
        return self.pip

    def _flat(self, key):
        usd = self.flat.get(key)
        return from_usd(usd, self.currency) if usd is not None else None

    async def get_load_balancer_monthly_price(self, region):
        return self._flat("lb")

    async def get_nat_gateway_monthly_price(self, region):
        return self._flat("nat")

    async def get_bastion_monthly_price(self, region, sku="Basic"):
        return self._flat("bastion")

    async def get_app_service_plan_monthly_price(self, region, sku):
        usd = self.asp.get((sku or "").lower())
        return from_usd(usd, self.currency) if usd is not None else None

    async def get_vm_monthly_price(self, region, sku):
        return self.vm_prices.get(sku)

    async def get_vm_reserved_monthly_price(self, region, sku, term="1 Year"):
        return self.ri.get((sku, term))

    async def get_vm_windows_monthly_price(self, region, sku):
        return self.win.get(sku)


def _disk(rid=DISK_RID):
    return {"id": rid, "name": "disk-1", "subscriptionId": "sub-1", "resourceGroup": "rg-a",
            "location": "eastus", "skuName": "Premium_LRS", "diskSizeGB": 128}


def _vm(
    max_cpu, peak_memory=None, memory_available=True, avg_cpu=1.0, datapoints=30,
    sku="Standard_D16s_v3", rid=VM_RID,
):
    return {"id": rid, "name": "vm-1", "subscriptionId": "sub-1", "resourceGroup": "rg-a",
            "location": "eastus", "vmSize": sku,
            "avg_cpu": avg_cpu, "max_cpu": max_cpu,
            "peak_memory_used_pct": peak_memory, "memory_available": memory_available,
            "cpu_datapoints": datapoints, "metric_window_days": 30}


# ── Pure scoring ─────────────────────────────────────────────────────────────────

def test_severity_from_savings_bands():
    assert severity_from_savings(500) == "critical"
    assert severity_from_savings(150) == "high"
    assert severity_from_savings(50) == "medium"
    assert severity_from_savings(5) == "low"
    # Severity is savings-only now: a tiny saving stays "low" even if Advisor rates it High, so a small
    # finding can never outrank a larger one. (Advisor's own rating is shown as a separate UI tag.)
    assert severity_from_savings(5, advisor_impact="High") == "low"


def test_severity_bands_are_currency_normalised():
    # USD bands: ₹500/mo ≈ $6 → low (NOT critical); ₹30,000/mo ≈ $360 → critical.
    assert severity_from_savings(500, currency="INR") == "low"
    assert severity_from_savings(30000, currency="INR") == "critical"
    # £150/mo ≈ $190 → high.
    assert severity_from_savings(150, currency="GBP") == "high"


async def test_orphan_estimate_converts_to_billing_currency():
    # The $32/mo NAT-gateway estimate must read as ~₹2,667 (not ₹32) on an INR assessment.
    engine = FindingsEngine(pricing=FakePricing(currency="INR"), currency="INR")
    f = (await engine.detect_orphans("idle_nat_gateways", [{"id": "/s/nat", "name": "nat"}]))[0]
    assert f["estimated_savings_monthly"] == round(32.0 / 0.012, 2)   # USD → INR
    assert f["severity"] == "medium"                                   # ~$32 → medium, not critical


def test_metrics_confidence_scales_with_datapoints():
    assert metrics_confidence(0) == 0.3
    assert metrics_confidence(7, 7) == 0.95
    assert metrics_confidence(3, 7) < metrics_confidence(7, 7)


# ── Unattached disk (ARG-authoritative) ─────────────────────────────────────────

async def test_unattached_disk_finding():
    engine = FindingsEngine(pricing=FakePricing())
    findings = await engine.detect_unattached_disks([_disk()])
    f = findings[0]
    assert f["category"] == "unattached_managed_disks"
    assert f["estimated_savings_monthly"] == 19.71
    assert f["estimated_savings_annual"] == round(19.71 * 12, 2)
    assert f["confidence"] >= 0.9
    assert f["debug_reason"] is None  # debug off by default


async def test_debug_reason_only_when_enabled():
    engine = FindingsEngine(pricing=FakePricing(), debug=True)
    f = (await engine.detect_unattached_disks([_disk()]))[0]
    assert f["debug_reason"] is not None
    assert "unattached" in f["debug_reason"].lower()


# ── Utilisation: idle / downsize (peak CPU + peak memory, real target SKU) ──────

async def test_idle_vm_requires_both_signals_low():
    # Peak CPU 3%, peak memory 6% — both under the idle bars → idle → saving = VM's actual billed cost.
    # Grounded-only: the VM must have measured per-resource cost (here $600, ≥ the $560.64 list price so
    # the saving reads as the full compute price, capped at what it actually costs).
    engine = FindingsEngine(pricing=FakePricing(), cost_map={VM_RID.lower(): 600.0})
    f = (await engine.detect_vm_utilisation_findings([_vm(max_cpu=3.0, peak_memory=6.0)]))[0]
    assert f["category"] == "idle_vms"
    assert f["estimated_savings_monthly"] == 560.64  # full D16s_v3 price, ≤ actual cost


async def test_spiky_cpu_vm_not_flagged_idle_or_downsized():
    # Real HyperV-Demo case: peak CPU 44% on a D16s_v3. Even D8 can't absorb it (44%*16/8=88%>70%)
    # → no candidate fits → well-utilised, left alone. This is the exact scenario that was
    # wrongly flagged "idle → delete" under the old average-CPU-only logic.
    engine = FindingsEngine(pricing=FakePricing(), cost_map={VM_RID.lower(): 600.0})
    findings = await engine.detect_vm_utilisation_findings(
        [_vm(max_cpu=44.0, peak_memory=30.0, avg_cpu=1.2)]
    )
    assert findings == []


async def test_high_memory_low_cpu_not_flagged_idle():
    # The "Redis cache" case: peak CPU only 2% (would look idle alone) but peak memory 85% —
    # real work is happening in memory. Must NOT be idle, and must NOT be downsizable either
    # (memory doesn't fit even on the next size down: 85%*64/32 = 170%).
    engine = FindingsEngine(pricing=FakePricing(), cost_map={VM_RID.lower(): 600.0})
    findings = await engine.detect_vm_utilisation_findings(
        [_vm(max_cpu=2.0, peak_memory=85.0)]
    )
    assert findings == []  # not idle (memory too high), not downsizable (no candidate fits)


async def test_downsize_recommends_real_target_sku_and_real_price_delta():
    # Peak CPU 15% (2.4 cores of 16), peak memory 20% (12.8 GB of 64) on a D16s_v3.
    # D8s_v3 fits both (30% CPU, 40% memory); D4s_v3 doesn't (60% CPU, 80% memory) → target = D8s_v3.
    engine = FindingsEngine(pricing=FakePricing(), cost_map={VM_RID.lower(): 600.0})
    f = (await engine.detect_vm_utilisation_findings(
        [_vm(max_cpu=15.0, peak_memory=20.0)]
    ))[0]
    assert f["category"] == "oversized_vms"
    assert f["details"]["current_sku"] == "Standard_D16s_v3"
    assert f["details"]["recommended_sku"] == "Standard_D8s_v3"
    # Absolute specs for the before/after visual.
    assert f["details"]["current_vcpu"] == 16
    assert f["details"]["recommended_vcpu"] == 8
    assert f["details"]["current_memory_gb"] == 64
    assert f["details"]["recommended_memory_gb"] == 32
    # Real price delta: 560.64 - 280.32, NOT a flat 50% heuristic.
    assert f["estimated_savings_monthly"] == 280.32
    assert "Resize from Standard_D16s_v3 to Standard_D8s_v3" in f["recommendation"]


async def test_no_candidate_fits_is_well_utilised():
    # Peak CPU 55% (8.8 cores) — even D8s_v3 can't absorb it (8.8/8=110%) → no finding.
    engine = FindingsEngine(pricing=FakePricing(), cost_map={VM_RID.lower(): 600.0})
    findings = await engine.detect_vm_utilisation_findings(
        [_vm(max_cpu=55.0, peak_memory=60.0)]
    )
    assert findings == []


async def test_memory_unavailable_still_allows_cpu_only_downsize_but_lower_confidence():
    # CPU very low (2%) but memory could NOT be measured. Must NOT be treated as idle (that was
    # exactly the earlier bug) — instead a conservative CPU-only downsize using the ladder, at
    # reduced confidence, with the caveat recorded.
    engine = FindingsEngine(pricing=FakePricing(),
                            cost_map={VM_RID.lower(): 600.0, (VM_RID + "-2").lower(): 600.0})
    with_memory = (await engine.detect_vm_utilisation_findings(
        [_vm(max_cpu=2.0, peak_memory=6.0, memory_available=True)]
    ))
    without_memory = (await engine.detect_vm_utilisation_findings(
        [_vm(max_cpu=2.0, peak_memory=None, memory_available=False, rid=VM_RID + "-2")]
    ))
    assert with_memory[0]["category"] == "idle_vms"  # both signals confirm idle
    assert without_memory[0]["category"] == "oversized_vms"  # NOT auto-deleted without proof
    assert without_memory[0]["details"]["memory_verified"] is False
    assert without_memory[0]["confidence"] < with_memory[0]["confidence"]


async def test_downsize_skipped_when_target_price_unavailable():
    # Target SKU price can't be fetched → skip rather than guess at savings.
    prices = dict(DEFAULT_VM_PRICES)
    prices["Standard_D8s_v3"] = None
    engine = FindingsEngine(pricing=FakePricing(vm_prices=prices), cost_map={VM_RID.lower(): 600.0})
    findings = await engine.detect_vm_utilisation_findings(
        [_vm(max_cpu=15.0, peak_memory=20.0)]
    )
    assert findings == []


async def test_vm_without_cpu_metrics_is_skipped():
    engine = FindingsEngine(pricing=FakePricing())
    findings = await engine.detect_vm_utilisation_findings([_vm(max_cpu=None, datapoints=0)])
    assert findings == []


async def test_vm_rightsizing_requires_per_resource_cost():
    # GROUNDED-ONLY: without per-resource billed cost we can't cap a list-price delta, so we never emit
    # an idle/oversized saving that could dwarf actual spend (the ₹318K-against-₹34K case). Empty
    # cost_map → no VM utilisation findings, even for a clearly oversized VM.
    engine = FindingsEngine(pricing=FakePricing())  # no cost_map
    assert await engine.detect_vm_utilisation_findings([_vm(max_cpu=15.0, peak_memory=20.0)]) == []
    # And an oversized saving is always capped at the VM's actual cost when that's smaller than the delta.
    partial = FindingsEngine(pricing=FakePricing(), cost_map={VM_RID.lower(): 40.0})  # partial billing
    f = (await partial.detect_vm_utilisation_findings([_vm(max_cpu=15.0, peak_memory=20.0)]))[0]
    assert f["estimated_savings_monthly"] == 40.0            # capped at actual, NOT the 280.32 list delta


async def test_no_finding_ever_exceeds_measured_spend():
    # ABSOLUTE GUARANTEE: even a list-price finding is clamped so it can never exceed the subscription's
    # total measured monthly spend — savings > spend must never be shown again.
    engine = FindingsEngine(
        pricing=FakePricing(),  # no cost_map → orphan LB uses the live retail price (18.0)
        measured_monthly_spend=5.0,  # tiny measured spend
    )
    f = (await engine.detect_orphans("empty_load_balancers", [_row("/s/lb-1", skuName="Standard")]))[0]
    assert f["estimated_savings_monthly"] == 5.0            # 18.0 live price clamped to measured spend
    assert f["details"]["savings_capped_at_measured_spend"] is True


# ── Commitments: Reserved Instances (authoritative — Azure reservation engine only) ──
# VM RIs (and every non-VM RI) come SOLELY from Azure's Consumption reservationRecommendations, parsed
# into groups and turned into findings by commitments_from_recommendations. The old retail-estimate VM
# detector (detect_vm_commitments) was retired: the Retail Prices API doesn't publish reservation
# prices for most VM SKUs, so it either fabricated a discount or under-covered. There is no longer any
# tool-computed RI discount, prod/nonprod guess, or retail RI fallback anywhere in the engine.

def _vm_ri_group(sku="Standard_D2s_v3", p1=100.0, p3=160.0, qty=3, region="eastus"):
    """A parsed VM reservationRecommendation group (what reservations.py produces for virtualmachines)."""
    terms = {}
    if p1 is not None:
        terms["P1Y"] = {"monthly_savings": p1, "monthly_ondemand": 400.0,
                        "monthly_reserved": round(400.0 - p1, 2), "quantity": qty}
    if p3 is not None:
        terms["P3Y"] = {"monthly_savings": p3, "monthly_ondemand": 400.0,
                        "monthly_reserved": round(400.0 - p3, 2), "quantity": qty}
    return {"resource_type": "virtualmachines", "category": "ri_vm", "product": "Virtual Machines",
            "sku": sku, "region": region, "scope": "Single", "flexibility_group": None,
            "subscription_id": "sub-1", "terms": terms}


def test_vm_ri_comes_from_reservation_engine_with_real_numbers():
    # A VM RI is surfaced ONLY because Azure's engine recommended it — using Azure's own SKU, quantity,
    # term and net savings. Nothing is computed by the tool.
    engine = FindingsEngine(pricing=FakePricing())
    f = engine.commitments_from_recommendations([_vm_ri_group(p1=100.0, p3=160.0, qty=3)])[0]
    assert f["category"] == "ri_vm"
    assert f["resource_id"] is None                       # aggregated → resource-less
    assert f["estimated_savings_monthly"] == 160.0        # Azure's 3-year net saving, verbatim
    assert f["details"]["total_1yr_monthly"] == 100.0     # Azure's 1-year net saving, verbatim
    assert f["details"]["source"] == "azure_reservation_recommendations"
    assert f["details"]["reservation_items"][0]["quantity"] == 3
    assert f["confidence"] >= 0.85                         # Azure-computed → high confidence


def test_vm_with_no_azure_ri_recommendation_produces_nothing():
    # Azure returned no VM reservation recommendation → we recommend no RI (never fabricate one),
    # even for a busy, steadily-running VM.
    engine = FindingsEngine(pricing=FakePricing())
    assert engine.commitments_from_recommendations([]) == []


def test_vm_ri_dropped_when_azure_reports_no_saving():
    # A VM group Azure returned but with no positive saving → not surfaced (no fabricated figure).
    empty = _vm_ri_group(p1=None, p3=None)
    assert FindingsEngine(pricing=FakePricing()).commitments_from_recommendations([empty]) == []


def test_savings_plan_vm_is_not_recommended():
    # We recommend Reserved Instances, not Savings Plans.
    sp = _vm_ri_group()
    sp["category"] = "savings_plan_vm"
    assert FindingsEngine(pricing=FakePricing()).commitments_from_recommendations([sp]) == []


# ── Commitments from Azure's own reservation engine (authoritative) ──────────────

def _ri_group(category="sql_db_reserved_capacity", sku="SQLDB_GP_Gen5", p1=100.0, p3=160.0, qty=2):
    # Non-VM reserved capacity (VMs are handled by the production-targeted VM detector, not here).
    terms = {}
    if p1 is not None:
        terms["P1Y"] = {"monthly_savings": p1, "monthly_ondemand": 400.0,
                        "monthly_reserved": round(400.0 - p1, 2), "quantity": qty}
    if p3 is not None:
        terms["P3Y"] = {"monthly_savings": p3, "monthly_ondemand": 400.0,
                        "monthly_reserved": round(400.0 - p3, 2), "quantity": qty}
    return {"resource_type": "sqldatabases", "category": category, "product": "SQL Database",
            "sku": sku, "region": "eastus", "scope": "Single", "flexibility_group": None,
            "subscription_id": "sub-1", "terms": terms}


def test_recommendations_include_vms_authoritatively():
    # VM reservation recs ARE surfaced here now — this is the ONLY (authoritative) source of VM RIs.
    engine = FindingsEngine(pricing=FakePricing())
    f = engine.commitments_from_recommendations([_ri_group(category="ri_vm", sku="Standard_D2s_v3")])[0]
    assert f["category"] == "ri_vm"
    assert f["estimated_savings_monthly"] == 160.0    # Azure's net saving, verbatim
    assert f["details"]["source"] == "azure_reservation_recommendations"


def test_recommendations_build_ri_finding_best_case_3yr():
    engine = FindingsEngine(pricing=FakePricing())
    f = engine.commitments_from_recommendations([_ri_group()])[0]
    assert f["category"] == "sql_db_reserved_capacity"
    assert f["resource_id"] is None                      # SKU-level purchase → escapes dedupe
    assert f["estimated_savings_monthly"] == 160.0       # best case = 3-year headline
    labels = [o["label"] for o in f["details"]["reservation_options"]]
    assert labels == ["3-year Reserved Instance", "1-year Reserved Instance"]  # best case first
    assert f["details"]["total_1yr_monthly"] == 100.0    # 1-year alternative still available
    assert f["details"]["source"] == "azure_reservation_recommendations"
    assert f["details"]["reservation_items"][0]["quantity"] == 2
    assert f["confidence"] >= 0.85                       # Azure-computed → high confidence


def test_recommendations_aggregate_multiple_vm_skus_into_one():
    engine = FindingsEngine(pricing=FakePricing())
    groups = [
        _ri_group(sku="Standard_D2s_v3", p1=100.0, p3=160.0),
        _ri_group(sku="Standard_D8s_v3", p1=50.0, p3=80.0),
    ]
    findings = engine.commitments_from_recommendations(groups)
    assert len(findings) == 1                            # one RI finding, not two
    f = findings[0]
    assert f["details"]["item_count"] == 2
    assert f["estimated_savings_monthly"] == 240.0       # best case 3yr: 160 + 80
    assert f["details"]["total_1yr_monthly"] == 150.0    # 1yr alternative: 100 + 50
    skus = {i["sku"] for i in f["details"]["reservation_items"]}
    assert skus == {"Standard_D2s_v3", "Standard_D8s_v3"}


def test_recommendations_map_sql_to_reserved_capacity_category():
    engine = FindingsEngine(pricing=FakePricing())
    g = _ri_group(category="sql_db_reserved_capacity", sku="SQLDB_BC_Gen5")
    g["resource_type"] = "sqldatabases"
    f = engine.commitments_from_recommendations([g])[0]
    assert f["category"] == "sql_db_reserved_capacity"
    assert f["display_name"] == "SQL Reserved Capacity"


def test_recommendations_skip_when_no_positive_saving():
    engine = FindingsEngine(pricing=FakePricing())
    empty = {"resource_type": "virtualmachines", "category": "ri_vm", "product": "Virtual Machines",
             "sku": "Standard_D2s_v3", "region": "eastus", "scope": "Single",
             "flexibility_group": None, "subscription_id": "sub-1", "terms": {}}
    assert engine.commitments_from_recommendations([empty]) == []


def test_no_ri_is_ever_computed_from_retail_prices():
    # Guard: even with full retail VM + reservation prices available, the engine surfaces NO VM RI
    # unless Azure's reservation engine recommended one. Retail RI pricing is never a source of RIs.
    engine = FindingsEngine(pricing=FakePricing())  # DEFAULT_RI has retail RI prices for some SKUs
    # Pass VM inventory nowhere near the reservation path; only reservation recs drive RIs.
    assert engine.commitments_from_recommendations([]) == []


# ── Windows Azure Hybrid Benefit (grounded, conditional, never fabricated) ───────
# AHB saving per VM = actual billed cost × (Windows − Linux)/Windows licence fraction, capped at the
# SKU's list licence premium. A VM with no live licence price OR no measured billing is EXCLUDED (never
# priced at list). The finding is always conditional on the customer owning the licences.

# Golden retail figures (Azure calculator, US): D16s v3 Windows $1,097.92, Linux $560.64 →
# licence premium $537.28, licence fraction 0.48936. D2s v3 Windows $137.24, Linux $70.08.
_AHB_PRICING = FakePricing(
    vm_prices={"Standard_D2s_v3": 70.08, "Standard_D16s_v3": 560.64},
    win={"Standard_D2s_v3": 137.24, "Standard_D16s_v3": 1097.92},
)
_D16_FRAC = (1097.92 - 560.64) / 1097.92


async def test_windows_ahb_saving_is_licence_share_of_actual_cost():
    # TEST 1: Windows VM with a live licence premium AND actual billing → saving = actual × fraction.
    vm = _vm(max_cpu=40.0, sku="Standard_D16s_v3", rid="/s/win"); vm["name"] = "win"
    engine = FindingsEngine(pricing=_AHB_PRICING, cost_map={"/s/win": 300.0})
    f = (await engine.detect_windows_ahb([vm]))[0]
    assert f["category"] == "windows_ahb"
    assert f["resource_id"] is None
    assert f["estimated_savings_monthly"] == round(300.0 * _D16_FRAC, 2)   # NOT 537.28, NOT 300
    assert f["estimated_savings_annual"] == round(round(300.0 * _D16_FRAC, 2) * 12, 2)
    assert f["validation_status"] == "validated"                          # grounded in actual cost
    d = f["details"]
    assert d["eligible_count"] == 1 and d["excluded_count"] == 0
    assert d["conditional"] is True and d["requires_license_ownership"] is True
    item = d["eligible_vms"][0]
    assert item["windows_price"] == 1097.92 and item["compute_only_price"] == 560.64
    assert item["licence_charge"] == 537.28                                # per-VM list premium (ref)


async def test_windows_ahb_never_exceeds_spend_on_partial_billing():
    # TEST 6 + the ₹888K bug: a partial-billing subscription must ground AHB in the SMALL actual spend,
    # never the full retail licence. Saving here is 5 × fraction ≈ 2.45, never 537.28.
    vm = _vm(max_cpu=40.0, sku="Standard_D16s_v3", rid="/s/win"); vm["name"] = "win"
    engine = FindingsEngine(
        pricing=_AHB_PRICING,
        cost_map={"/s/win": 5.0},                                          # only a few hours billed so far
        cost_consistency={"/s/win": {"billed_months": 1, "stable": False}},
    )
    f = (await engine.detect_windows_ahb([vm]))[0]
    assert f["estimated_savings_monthly"] == round(5.0 * _D16_FRAC, 2)     # ≈ 2.45, NOT 537.28
    assert f["estimated_savings_monthly"] <= 5.0                           # can never exceed the bill
    assert f["details"]["partial_billing"] is True
    assert "billed SO FAR" in f["description"]


async def test_windows_ahb_excludes_vm_with_no_billing_never_fabricates():
    # TEST 7: no billing data → cannot ground → VM excluded, NO fabricated saving (empty finding).
    vm = _vm(max_cpu=40.0, sku="Standard_D16s_v3", rid="/s/win"); vm["name"] = "win"
    engine = FindingsEngine(pricing=_AHB_PRICING, cost_map={"/s/other": 100.0})  # win not billed
    assert await engine.detect_windows_ahb([vm]) == []


async def test_windows_ahb_excludes_vm_with_no_licence_price():
    # TEST 2 + 8: no live Windows/Linux price for the SKU → can't establish the premium → excluded,
    # never estimated from a percentage. (win price present but not > linux → no premium.)
    vm = _vm(max_cpu=40.0, sku="Standard_Zz9", rid="/s/win"); vm["name"] = "win"
    engine = FindingsEngine(pricing=FakePricing(vm_prices={"Standard_Zz9": 100.0}, win={}),
                            cost_map={"/s/win": 300.0})
    assert await engine.detect_windows_ahb([vm]) == []


async def test_windows_ahb_reports_excluded_counts():
    # A mixed fleet: one groundable VM + one with no billing + one with no price → total reconciles to
    # the single eligible VM, and the excluded VMs are surfaced (not silently dropped, not counted).
    ok = _vm(max_cpu=40.0, sku="Standard_D16s_v3", rid="/s/ok"); ok["name"] = "ok"
    nobill = _vm(max_cpu=40.0, sku="Standard_D2s_v3", rid="/s/nobill"); nobill["name"] = "nobill"
    noprice = _vm(max_cpu=40.0, sku="Standard_Zz9", rid="/s/noprice"); noprice["name"] = "noprice"
    engine = FindingsEngine(
        pricing=FakePricing(vm_prices={"Standard_D2s_v3": 70.08, "Standard_D16s_v3": 560.64,
                                       "Standard_Zz9": 100.0},
                            win={"Standard_D2s_v3": 137.24, "Standard_D16s_v3": 1097.92}),
        cost_map={"/s/ok": 300.0})  # nobill & noprice absent / unpriced
    f = (await engine.detect_windows_ahb([ok, nobill, noprice]))[0]
    assert [v["name"] for v in f["details"]["eligible_vms"]] == ["ok"]
    assert f["estimated_savings_monthly"] == round(300.0 * _D16_FRAC, 2)
    assert f["details"]["excluded_count"] == 2
    assert f["details"]["excluded_no_billing"] == 1
    assert f["details"]["excluded_no_pricing"] == 1


async def test_windows_ahb_aggregate_equals_sum_of_vm_level():
    # TEST 5: aggregate saving == sum of the validated per-VM savings.
    vms = [_vm(max_cpu=40.0, sku="Standard_D2s_v3", rid="/s/a"),
           _vm(max_cpu=40.0, sku="Standard_D16s_v3", rid="/s/b")]
    vms[0]["name"], vms[1]["name"] = "win-a", "win-b"
    engine = FindingsEngine(pricing=_AHB_PRICING, cost_map={"/s/a": 100.0, "/s/b": 400.0})
    f = (await engine.detect_windows_ahb(vms))[0]
    per_vm = [v["monthly_savings"] for v in f["details"]["eligible_vms"]]
    assert f["estimated_savings_monthly"] == round(sum(per_vm), 2)
    assert f["details"]["eligible_count"] == 2


async def test_windows_ahb_uses_each_vms_own_licence_delta_per_family():
    # The Windows Server licence is NOT uniform per vCore across families: a B-series (burstable) VM
    # carries a far lower licence per vCore than a D-series one. Each VM uses its OWN (Windows − Linux)
    # delta; a fleet average would inflate the B-series ~11× (a real bug this guards against).
    pricing = FakePricing(
        vm_prices={"Standard_D4s_v3": 158.76, "Standard_B2ms": 60.0},
        win={"Standard_D4s_v3": 311.00,   # licence 152.24 → 38.06/vCore (D-series, high)
             "Standard_B2ms": 66.60},     # licence   6.60 →  3.30/vCore (B-series, low)
    )
    vms = []
    for i, sku in enumerate(("Standard_D4s_v3", "Standard_B2ms")):
        vm = _vm(max_cpu=40.0, sku=sku, rid=f"/s/vm-{i}"); vm["name"] = f"win-{i}"
        vms.append(vm)
    engine = FindingsEngine(pricing=pricing, cost_map={"/s/vm-0": 500.0, "/s/vm-1": 500.0})
    out = await engine.detect_windows_ahb(vms)
    items = {v["name"]: v for v in out[0]["details"]["eligible_vms"]}
    assert items["win-0"]["licence_charge"] == 152.24        # D4s_v3 (per-VM list premium)
    assert items["win-1"]["licence_charge"] == 6.60          # B2ms stays low
    assert round(items["win-0"]["licence_charge"] / items["win-0"]["vcpu"], 2) == 38.06
    assert round(items["win-1"]["licence_charge"] / items["win-1"]["vcpu"], 2) == 3.30


async def test_windows_ahb_linux_vm_never_included():
    # TEST 4: a VM with no Windows licence premium (Windows price == Linux price) → excluded, never AHB.
    vm = _vm(max_cpu=40.0, sku="Standard_D2s_v3", rid="/s/lin"); vm["name"] = "lin"
    engine = FindingsEngine(
        pricing=FakePricing(vm_prices={"Standard_D2s_v3": 70.08}, win={"Standard_D2s_v3": 70.08}),
        cost_map={"/s/lin": 300.0})
    assert await engine.detect_windows_ahb([vm]) == []


async def test_windows_ahb_empty_when_no_eligible_vms():
    engine = FindingsEngine(pricing=FakePricing())
    assert await engine.detect_windows_ahb([]) == []


async def test_grounded_aggregate_reads_as_validated_not_estimate():
    # A grounded AHB/commitment finding must read as cost-validated — not an estimate badge.
    vm = _vm(max_cpu=40.0, sku="Standard_D2s_v3", rid=VM_RID); vm["name"] = "win"
    grounded_ahb = (await FindingsEngine(pricing=_AHB_PRICING, cost_map={VM_RID.lower(): 100.0})
                    .detect_windows_ahb([vm]))[0]
    assert grounded_ahb["validation_status"] == "validated"
    # An RI finding from Azure's engine is grounded (real usage at real prices) → validated.
    ri = FindingsEngine(pricing=FakePricing()).commitments_from_recommendations([_vm_ri_group()])[0]
    assert ri["validation_status"] == "validated"


async def test_windows_ahb_excludes_deleted_vms():
    # A Windows VM already recommended for deletion (idle) must NOT also earn an AHB licence saving.
    keep = _vm(max_cpu=40.0, sku="Standard_D2s_v3", rid="/s/keep"); keep["name"] = "keep"
    dele = _vm(max_cpu=2.0, sku="Standard_D16s_v3", rid="/s/idle"); dele["name"] = "idle"
    engine = FindingsEngine(pricing=_AHB_PRICING, cost_map={"/s/keep": 100.0, "/s/idle": 400.0})
    out = await engine.detect_windows_ahb([keep, dele], exclude_ids={"/s/idle"})
    assert [v["name"] for v in out[0]["details"]["eligible_vms"]] == ["keep"]


def test_deallocated_vm_quantifies_attached_disk_cost():
    # A stopped VM's disks keep billing; deleting the VM+disks saves their real cost (from Cost Mgmt).
    disk_os = "/subscriptions/s/rg/providers/microsoft.compute/disks/osdisk"
    disk_data = "/subscriptions/s/rg/providers/microsoft.compute/disks/datadisk"
    vm = {
        "id": "/s/vm-dealloc", "name": "old-vm", "subscriptionId": "s", "location": "eastus",
        "vmSize": "Standard_D2s_v3", "powerState": "VM deallocated",
        "osDiskId": disk_os, "dataDisks": [{"managedDisk": {"id": disk_data}}],
    }
    engine = FindingsEngine(pricing=FakePricing(), cost_map={disk_os.lower(): 12.5, disk_data.lower(): 30.0})
    out = engine.detect_deallocated_vms([vm])
    assert len(out) == 1
    f = out[0]
    assert f["category"] == "deallocated_vms"
    assert f["estimated_savings_monthly"] == 42.5          # 12.5 + 30.0 (the two attached disks)
    assert f["validation_status"] == "validated"           # grounded in the disks' actual cost
    assert f["details"]["attached_disk_count"] == 2
    assert f["details"]["disk_monthly_cost"] == 42.5


def test_deallocated_vm_is_zero_without_cost_data():
    # No per-resource billing → can't quantify the disk cost → 0 (dropped by the zero-savings filter).
    vm = {"id": "/s/vm", "name": "vm", "vmSize": "Standard_D2s_v3", "powerState": "VM deallocated",
          "osDiskId": "/s/disk", "dataDisks": []}
    f = FindingsEngine(pricing=FakePricing()).detect_deallocated_vms([vm])[0]
    assert f["estimated_savings_monthly"] == 0.0


def test_paused_sql_db_grounds_in_actual_cost():
    # A paused SQL DB's saving is its ACTUAL billed (storage) cost — not $0, not a guess.
    rid = "/subscriptions/s/rg/providers/microsoft.sql/servers/srv/databases/db1"
    engine = FindingsEngine(pricing=FakePricing(), cost_map={rid.lower(): 45.0})
    f = engine.detect_paused_sql_databases([{"id": rid, "name": "db1", "status": "Paused"}])[0]
    assert f["category"] == "paused_sql_databases"
    assert f["estimated_savings_monthly"] == 45.0
    assert f["validation_status"] == "validated"


def test_paused_sql_db_without_cost_is_zero_and_dropped():
    # No billed cost → 0 (dropped by the pipeline zero-filter) rather than a fabricated $0 opportunity.
    rid = "/subscriptions/s/rg/providers/microsoft.sql/servers/srv/databases/db1"
    f = FindingsEngine(pricing=FakePricing()).detect_paused_sql_databases(
        [{"id": rid, "name": "db1", "status": "Paused"}])[0]
    assert f["estimated_savings_monthly"] == 0.0


def test_stopped_sql_mi_grounds_in_actual_cost():
    rid = "/subscriptions/s/rg/providers/microsoft.sql/managedinstances/mi1"
    engine = FindingsEngine(pricing=FakePricing(), cost_map={rid.lower(): 1200.0})
    f = engine.detect_stopped_sql_managed_instances([{"id": rid, "name": "mi1", "state": "Stopped"}])[0]
    assert f["category"] == "stopped_sql_managed_instances"
    assert f["estimated_savings_monthly"] == 1200.0
    assert f["validation_status"] == "validated"


def test_asp_downsize_target_respects_headroom():
    from app.services.findings import find_asp_downsize_target
    # S3 (4 cores) @ 12% CPU / 15% mem → S1: projected 48% CPU, 60% mem ≤ 70% → smallest safe = S1.
    assert find_asp_downsize_target("S3", 12.0, 15.0)[0] == "s1"
    # @ 25% CPU, S1 would project 100% (unsafe) → next up S2 (50%) is the smallest safe.
    assert find_asp_downsize_target("S3", 25.0, 10.0)[0] == "s2"
    assert find_asp_downsize_target("S2", 60.0, 20.0) is None   # busy → no downsize
    assert find_asp_downsize_target("S1", 5.0, 5.0) is None     # already smallest in series
    assert find_asp_downsize_target("Y99", 5.0, 5.0) is None    # unknown SKU


async def test_detect_app_service_rightsizing_grounded_in_price_delta():
    engine = FindingsEngine(pricing=FakePricing())
    plan = {"id": "/s/asp-1", "name": "web-plan", "subscriptionId": "s", "location": "eastus",
            "skuName": "S3", "max_cpu_pct": 12.0, "max_memory_pct": 15.0,
            "metric_datapoints": 30, "metric_window_days": 30}
    out = await engine.detect_app_service_rightsizing([plan])
    assert len(out) == 1
    f = out[0]
    assert f["category"] == "app_service_plan_rightsizing"
    assert f["estimated_savings_monthly"] == round(227.76 - 56.94, 2)  # S3 → S1 price delta
    assert f["details"]["current_sku"] == "S3" and f["details"]["recommended_sku"] == "s1"


async def test_app_service_rightsizing_skips_busy_and_unmetered():
    engine = FindingsEngine(pricing=FakePricing())
    busy = {"id": "/s/a", "skuName": "S2", "location": "eastus", "max_cpu_pct": 65.0,
            "max_memory_pct": 30.0, "metric_datapoints": 30}
    unmetered = {"id": "/s/b", "skuName": "S3", "location": "eastus", "max_cpu_pct": None,
                 "metric_datapoints": 0}
    assert await engine.detect_app_service_rightsizing([busy, unmetered]) == []


def test_sql_vcore_target_needs_all_metrics_under_ceiling():
    from app.services.findings import find_sql_vcore_target
    # 8 vCores, all peaks ~15% → 2 vCores projects 60% ≤ 70% → smallest safe = 2.
    assert find_sql_vcore_target(8, [15.0, 12.0, 10.0]) == 2
    # log IO 40% blocks the 4x jump to 2 (would be 160%); 4 vCores (2x → 80%) also fails; none safe?
    # 40 at 8→4 (×2)=80>70 fail; 8→6(×1.33)=53<=70 but data/cpu must also clear: cpu 20×1.33=27 ok → 6.
    assert find_sql_vcore_target(8, [20.0, 20.0, 40.0]) == 6
    assert find_sql_vcore_target(4, [50.0, 10.0, 10.0]) is None   # CPU too high to shrink
    assert find_sql_vcore_target(2, [5.0, 5.0, 5.0]) is None      # already smallest
    assert find_sql_vcore_target(8, [None, None, None]) is None   # no metrics at all


async def test_detect_sql_db_rightsizing_grounded_in_actual_cost():
    rid = "/subscriptions/s/rg/providers/microsoft.sql/servers/srv/databases/db1"
    engine = FindingsEngine(pricing=FakePricing(), cost_map={rid.lower(): 800.0})
    db = {"id": rid, "name": "db1", "subscriptionId": "s", "location": "eastus",
          "tier": "GeneralPurpose", "skuName": "GP_Gen5", "vcores": 8,
          "max_cpu_pct": 12.0, "max_data_io_pct": 10.0, "max_log_io_pct": 8.0,
          "metric_datapoints": 30, "metric_window_days": 30}
    out = await engine.detect_sql_db_rightsizing([db])
    assert len(out) == 1
    f = out[0]
    assert f["category"] == "sql_db_rightsizing"
    # 8 → 2 vCores; saving = 800 × (8-2)/8 = 600, grounded in the DB's real bill.
    assert f["estimated_savings_monthly"] == 600.0
    assert f["validation_status"] == "validated"
    assert f["details"]["current_vcpu"] == 8 and f["details"]["recommended_vcpu"] == 2


async def test_sql_db_rightsizing_requires_cost_data():
    # No Cost Management data → we don't guess a downsize on a stateful DB.
    db = {"id": "/s/db", "tier": "GeneralPurpose", "skuName": "GP_Gen5", "vcores": 8,
          "max_cpu_pct": 5.0, "max_data_io_pct": 5.0, "max_log_io_pct": 5.0, "metric_datapoints": 30}
    assert await FindingsEngine(pricing=FakePricing()).detect_sql_db_rightsizing([db]) == []


async def test_detect_disk_rightsizing_premium_to_standard():
    # FakePricing.disk is flat 19.71 for any sku/size, so premium−standard would be 0 → give a pricier
    # premium via a custom fake to exercise the delta.
    class DiskFake(FakePricing):
        async def get_managed_disk_monthly_price(self, region, sku, size):
            return 40.0 if "premium" in sku.lower() else 12.0
    disk = {"id": "/s/disk-1", "name": "data-1", "subscriptionId": "s", "location": "eastus",
            "skuName": "Premium_LRS", "sizeGB": 256,
            "peak_iops": 120.0, "peak_mbps": 15.0, "metric_datapoints": 30, "metric_window_days": 30}
    out = await FindingsEngine(pricing=DiskFake()).detect_disk_rightsizing([disk])
    assert len(out) == 1
    f = out[0]
    assert f["category"] == "disk_rightsizing"
    assert f["estimated_savings_monthly"] == round(40.0 - 12.0, 2)   # 28.0
    assert f["details"]["recommended_sku"] == "StandardSSD_LRS"


async def test_disk_rightsizing_skips_busy_and_unmetered():
    class DiskFake(FakePricing):
        async def get_managed_disk_monthly_price(self, region, sku, size):
            return 40.0 if "premium" in sku.lower() else 12.0
    busy = {"id": "/s/a", "skuName": "Premium_LRS", "location": "eastus", "sizeGB": 128,
            "peak_iops": 900.0, "peak_mbps": 20.0, "metric_datapoints": 30}   # IOPS > 350 → skip
    unmetered = {"id": "/s/b", "skuName": "Premium_LRS", "location": "eastus", "sizeGB": 128,
                 "peak_iops": None, "peak_mbps": None, "metric_datapoints": 0}
    assert await FindingsEngine(pricing=DiskFake()).detect_disk_rightsizing([busy, unmetered]) == []


async def test_disk_rightsizing_excludes_sql_vm_disks():
    # A low-IOPS Premium disk that would otherwise be flagged, but it's attached to a SQL VM → SQL needs
    # Premium's latency even at low IOPS, so it must NOT be recommended for downgrade.
    class DiskFake(FakePricing):
        async def get_managed_disk_monthly_price(self, region, sku, size):
            return 40.0 if "premium" in sku.lower() else 12.0
    vm_id = "/subscriptions/s/rg/providers/microsoft.compute/virtualmachines/sqlvm"
    disk = {"id": "/s/sqldisk", "skuName": "Premium_LRS", "location": "eastus", "sizeGB": 256,
            "managedBy": vm_id, "peak_iops": 80.0, "peak_mbps": 10.0, "metric_datapoints": 30}
    out = await FindingsEngine(pricing=DiskFake()).detect_disk_rightsizing(
        [disk], exclude_vm_ids={vm_id.lower()})
    assert out == []


async def test_detect_sql_mi_rightsizing_uses_mi_ladder_and_cpu():
    rid = "/subscriptions/s/rg/providers/microsoft.sql/managedinstances/mi1"
    engine = FindingsEngine(pricing=FakePricing(), cost_map={rid.lower(): 2000.0})
    mi = {"id": rid, "name": "mi1", "subscriptionId": "s", "location": "eastus",
          "tier": "GeneralPurpose", "skuName": "GP_Gen5", "vcores": 16,
          "max_cpu_pct": 12.0, "metric_datapoints": 30, "metric_window_days": 30}
    out = await engine.detect_sql_mi_rightsizing([mi])
    assert len(out) == 1
    f = out[0]
    assert f["category"] == "sql_mi_rightsizing"
    # MI ladder: 16 → 4 (ratio 4 → 48% ≤ 70%). saving = 2000 × (16-4)/16 = 1500.
    assert f["details"]["recommended_vcpu"] == 4
    assert f["estimated_savings_monthly"] == 1500.0
    assert f["validation_status"] == "validated"


async def test_windows_ahb_grounds_saving_in_actual_cost():
    # Retail: D2s_v3 Windows 137.24, Linux 70.08 → licence fraction 67.16/137.24 ≈ 0.4893.
    # With a REAL cost of $50/mo, AHB = 50 × 0.4893 ≈ 24.47 (not the raw $67.16 list delta).
    vm = _vm(max_cpu=40.0, sku="Standard_D2s_v3")
    vm["name"] = "winvm"
    engine = FindingsEngine(pricing=FakePricing(), cost_map={VM_RID.lower(): 50.0})
    f = (await engine.detect_windows_ahb([vm]))[0]
    frac = (137.24 - 70.08) / 137.24
    assert f["estimated_savings_monthly"] == round(50.0 * frac, 2)
    assert f["estimated_savings_monthly"] < (137.24 - 70.08)   # grounded < raw list delta
    assert f["details"]["eligible_vms"][0]["actual_cost_based"] is True


async def test_windows_ahb_skips_non_billing_vms():
    # Two Windows VMs; only one has measured cost. When per-resource billing is available, the VM
    # that isn't billing (deallocated / demo box) must be EXCLUDED — you can't save on a $0 VM.
    billing = _vm(max_cpu=40.0, sku="Standard_D2s_v3", rid="/s/vm-billing")
    billing["name"] = "billing-vm"
    idle_demo = _vm(max_cpu=40.0, sku="Standard_D16s_v3", rid="/s/vm-demo")
    idle_demo["name"] = "demo-vm"
    engine = FindingsEngine(pricing=FakePricing(), cost_map={"/s/vm-billing": 60.0})
    out = await engine.detect_windows_ahb([billing, idle_demo])
    assert len(out) == 1
    names = [v["name"] for v in out[0]["details"]["eligible_vms"]]
    assert names == ["billing-vm"]            # demo-vm (no cost) excluded
    assert out[0]["details"]["eligible_count"] == 1


async def test_windows_ahb_excluded_entirely_without_any_billing():
    # With NO billing data at all, AHB cannot be grounded → the finding is empty. We never fall back to
    # the raw retail licence delta (that was the source of the ₹888K-against-₹44K-spend bug).
    vm = _vm(max_cpu=40.0, sku="Standard_D2s_v3")
    engine = FindingsEngine(pricing=FakePricing())  # no cost_map
    assert await engine.detect_windows_ahb([vm]) == []


async def test_geo_redundant_vault_produces_no_fabricated_saving():
    # RETIRED: the GRS→LRS backup-redundancy saving can't be isolated from the vault's total bill
    # without an assumption, and Resource Graph doesn't expose backup volume — so no dollar figure is
    # produced (the bucket is no longer a rule).
    engine = FindingsEngine(pricing=FakePricing())
    assert await engine.detect_orphans("geo_redundant_vaults", [
        {"id": "/s/vault-1", "name": "vault-1", "subscriptionId": "sub-1", "resourceGroup": "rg-a"},
    ]) == []


# ── Broad-coverage orphan/waste (rule-driven) ────────────────────────────────────

def _row(rid, **extra):
    return {"id": rid, "name": "res-1", "subscriptionId": "sub-1", "resourceGroup": "rg-a",
            "location": "eastus", **extra}


async def test_orphan_snapshot_grounded_in_actual_cost():
    # Snapshots are quantified ONLY from their actual Cost Management billed cost (they bill on
    # incremental used storage, not provisioned size) — never a per-GB estimate.
    engine = FindingsEngine(pricing=FakePricing(), cost_map={"/s/snap-1": 7.40})
    f = (await engine.detect_orphans("orphaned_snapshots", [_row("/s/snap-1", diskSizeGB=200)]))[0]
    assert f["category"] == "orphaned_snapshots"
    assert f["estimated_savings_monthly"] == 7.40           # the real bill, not 200 × per-GB
    assert f["validation_status"] == "validated"            # grounded in actual cost


async def test_orphan_snapshot_without_cost_data_is_dropped():
    # No actual cost → no fabricated per-GB estimate → the row is skipped entirely.
    engine = FindingsEngine(pricing=FakePricing())          # empty cost_map
    assert await engine.detect_orphans("orphaned_snapshots", [_row("/s/snap-1", diskSizeGB=200)]) == []


async def test_empty_load_balancer_priced_live():
    engine = FindingsEngine(pricing=FakePricing())
    f = (await engine.detect_orphans("empty_load_balancers", [_row("/s/lb-1", skuName="Standard")]))[0]
    assert f["category"] == "empty_load_balancers"
    assert f["estimated_savings_monthly"] == 18.0           # live Retail Prices rate


async def test_idle_nat_gateway_priced_live():
    engine = FindingsEngine(pricing=FakePricing())
    f = (await engine.detect_orphans("idle_nat_gateways", [_row("/s/nat-1")]))[0]
    assert f["category"] == "idle_nat_gateways"
    assert f["estimated_savings_monthly"] == 32.0           # live Retail Prices rate


async def test_flat_orphan_dropped_when_no_live_price():
    # No live retail price and no actual cost → don't quantify (finding dropped), never a fallback.
    engine = FindingsEngine(pricing=FakePricing(flat={}))   # get_*_monthly_price → None
    assert await engine.detect_orphans("empty_load_balancers", [_row("/s/lb-1")]) == []


async def test_flat_orphan_prefers_actual_cost_over_live():
    # When Cost Management has the resource's real cost, that grounds the saving (and caps the live rate).
    engine = FindingsEngine(pricing=FakePricing(), cost_map={"/s/nat-1": 12.0})
    f = (await engine.detect_orphans("idle_nat_gateways", [_row("/s/nat-1")]))[0]
    assert f["estimated_savings_monthly"] == 12.0           # actual billed cost, not the 32.0 live rate
    assert f["validation_status"] == "validated"


async def test_orphan_finding_keys_match_model():
    engine = FindingsEngine(pricing=FakePricing())
    f = (await engine.detect_orphans("idle_nat_gateways", [_row("/s/nat-1")]))[0]
    assert set(f.keys()) <= FINDING_COLUMNS


async def test_unknown_orphan_bucket_returns_empty():
    engine = FindingsEngine(pricing=FakePricing())
    assert await engine.detect_orphans("not_a_bucket", [_row("/s/x")]) == []


# ── Advisor correlation + validation ────────────────────────────────────────────

def _advisor_rec(resource_id, monthly=120.0, rec_id="ADV-1"):
    return {
        "id": rec_id,
        "properties": {
            "impact": "High",
            "impactedField": "Microsoft.Compute/virtualMachines",
            "impactedValue": "vm-1",
            "shortDescription": {"problem": "Buy a reserved instance", "solution": "Purchase RI"},
            "extendedProperties": {"savingsAmount": str(monthly), "annualSavingsAmount": str(monthly * 12)},
            "resourceMetadata": {"resourceId": resource_id},
        },
    }


async def test_advisor_finding_and_correlation_index():
    recs = [_advisor_rec(VM_RID)]
    index = build_advisor_index(recs)
    engine = FindingsEngine(pricing=FakePricing(), advisor_index=index)
    f = engine.advisor_findings(recs)[0]
    assert f["category"] == "advisor_cost"
    assert f["advisor_recommendation_id"] == "ADV-1"
    assert f["severity"] == "high"  # 120/mo is the 100–300 "high" band


async def test_sql_ahb_is_retired_never_fabricates_a_saving():
    # SQL AHB is RETIRED: the SQL licence component can't be reliably derived from any Microsoft
    # pricing API (the Retail Prices API exposes only ONE compute price per vCore, no separate
    # base/AHB meter), so we surface NO SQL AHB figure — even with full cost data available.
    db_rid = "/subscriptions/s/rg/providers/microsoft.sql/servers/srv/databases/db1"
    mi_rid = "/subscriptions/s/rg/providers/microsoft.sql/managedinstances/mi1"
    cost_map = {db_rid.lower(): 2000.0, mi_rid.lower(): 5000.0}
    engine = FindingsEngine(pricing=FakePricing(), cost_map=cost_map)
    resources = [
        {"id": db_rid, "name": "db1", "subscriptionId": "s", "location": "eastus",
         "skuName": "GP_Gen5", "tier": "GeneralPurpose", "vcores": 8},
        {"id": mi_rid, "name": "mi1", "subscriptionId": "s", "location": "eastus",
         "skuName": "GP_Gen5", "tier": "GeneralPurpose", "vcores": 16},
    ]
    assert await engine.detect_sql_ahb(resources) == []


async def test_zero_savings_findings_are_filtered_from_pipeline():
    """A ₹0/$0 finding (e.g. an Advisor cost rec Azure returns with no savings figure) is noise in a
    savings report — the pipeline must drop it before persisting, keeping only real opportunities."""
    recs = [
        _advisor_rec(VM_RID, monthly=0.0, rec_id="ADV-ZERO"),   # no quantified saving → drop
        _advisor_rec(DISK_RID, monthly=50.0, rec_id="ADV-KEEP"),  # real saving → keep
    ]
    engine = FindingsEngine(pricing=FakePricing(), advisor_index=build_advisor_index(recs))
    out = await pipeline._detect_all(engine, {}, [], recs)
    kept = {f["advisor_recommendation_id"] for f in out}
    assert kept == {"ADV-KEEP"}
    assert all((f.get("estimated_savings_monthly") or 0) > 0 for f in out)


async def test_inventory_finding_correlates_with_advisor_id():
    # An Advisor rec exists for the same disk → our disk finding should carry the advisor id.
    recs = [_advisor_rec(DISK_RID)]
    engine = FindingsEngine(pricing=FakePricing(), advisor_index=build_advisor_index(recs))
    f = (await engine.detect_unattached_disks([_disk()]))[0]
    assert f["advisor_recommendation_id"] == "ADV-1"


async def test_estimate_over_actual_is_capped_and_validated():
    # Disk "saves" 19.71 but actual cost is only $5 → cap to $5. The figure is now the real cost, so
    # it reads as validated (NOT a scary "needs review +294%" on a number that's now exactly right).
    engine = FindingsEngine(pricing=FakePricing(), cost_map={DISK_RID.lower(): 5.0})
    f = (await engine.detect_unattached_disks([_disk()]))[0]
    assert f["estimated_savings_monthly"] == 5.0            # capped to actual
    assert f["validation_status"] == "validated"
    assert f["details"].get("savings_capped_at_actual_cost") is True


async def test_validation_validated_within_tolerance():
    engine = FindingsEngine(pricing=FakePricing(), cost_map={DISK_RID.lower(): 19.0})
    f = (await engine.detect_unattached_disks([_disk()]))[0]
    assert f["validation_status"] == "validated"
    assert f["actual_monthly_cost"] == 19.0


async def test_savings_capped_at_actual_cost():
    # Disk priced at 19.71 but it actually costs only $5/mo → savings clamped to $5.
    engine = FindingsEngine(pricing=FakePricing(), cost_map={DISK_RID.lower(): 5.0})
    f = (await engine.detect_unattached_disks([_disk()]))[0]
    assert f["estimated_savings_monthly"] == 5.0
    assert f["estimated_savings_annual"] == 60.0
    assert f["details"].get("savings_capped_at_actual_cost") is True


async def test_savings_not_capped_when_estimate_below_actual():
    # Estimate 19.71 < actual 30 → no cap.
    engine = FindingsEngine(pricing=FakePricing(), cost_map={DISK_RID.lower(): 30.0})
    f = (await engine.detect_unattached_disks([_disk()]))[0]
    assert f["estimated_savings_monthly"] == 19.71
    assert "savings_capped_at_actual_cost" not in f["details"]


# ── Output shape matches the DB model ────────────────────────────────────────────

async def test_finding_keys_match_model_columns():
    engine = FindingsEngine(pricing=FakePricing())
    f = (await engine.detect_unattached_disks([_disk()]))[0]
    assert set(f.keys()) <= FINDING_COLUMNS, set(f.keys()) - FINDING_COLUMNS
