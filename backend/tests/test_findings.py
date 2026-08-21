"""Tests for the findings engine (Step 6)."""
from __future__ import annotations

from app.models.db import Finding
from app.services import assessment as pipeline
from app.services.currency import from_usd
from app.services.findings import (
    FindingsEngine,
    advisor_rec_is_subscription_scoped,
    build_advisor_index,
    metrics_confidence,
    severity_from_savings,
)
from app.services.reservations import (
    parse_reservation_recommendations,
    reconcile_sql_recommendations,
    reconcile_vm_recommendations,
    sql_purchasing_model,
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


async def test_orphan_reference_price_converts_to_billing_currency():
    # With NO actual billed cost, the NAT gateway is a REVIEW finding (never a saving). The $32/mo retail
    # rate is shown only as a REFERENCE price, and must read as ~₹2,667 (not ₹32) on an INR assessment.
    engine = FindingsEngine(pricing=FakePricing(currency="INR"), currency="INR")
    f = (await engine.detect_orphans("idle_nat_gateways", [{"id": "/s/nat", "name": "nat"}]))[0]
    assert f["evidence_state"] == "review"
    assert f["estimated_savings_monthly"] == 0.0                       # REVIEW: never a saving
    assert f["details"]["reference_monthly_price"] == round(32.0 / 0.012, 2)   # USD → INR, reference only
    assert f["details"]["reference_price_source"] == "AUTHORITATIVE_RETAIL_PRICE"


def test_metrics_confidence_scales_with_datapoints():
    assert metrics_confidence(0) == 0.3
    assert metrics_confidence(7, 7) == 0.95
    assert metrics_confidence(3, 7) < metrics_confidence(7, 7)


# ── Unattached disk (ARG-authoritative) ─────────────────────────────────────────

async def test_unattached_disk_finding_grounded_in_actual_cost():
    # With the disk's ACTUAL billed cost available, the saving is QUANTIFIED and grounded in that cost.
    engine = FindingsEngine(pricing=FakePricing(), cost_map={DISK_RID.lower(): 19.71})
    findings = await engine.detect_unattached_disks([_disk()])
    f = findings[0]
    assert f["category"] == "unattached_managed_disks"
    assert f["evidence_state"] == "quantified"
    assert f["estimated_savings_monthly"] == 19.71                     # = actual billed cost
    assert f["estimated_savings_annual"] == round(19.71 * 12, 2)
    assert f["details"]["savings_source"] == "ACTUAL_BILLED_COST"
    assert f["debug_reason"] is None  # debug off by default


async def test_unattached_disk_without_billed_cost_is_review_only():
    # No actual billed cost → the disk is a REVIEW finding: the retail rate is a clearly-labelled
    # reference, NEVER a saving. This is the no-fabrication rule (list price ≠ customer spend/savings).
    engine = FindingsEngine(pricing=FakePricing())               # empty cost_map
    f = (await engine.detect_unattached_disks([_disk()]))[0]
    assert f["evidence_state"] == "review"
    assert f["estimated_savings_monthly"] == 0.0
    assert f["details"]["financial_impact"] == "Not quantified"
    assert f["details"]["reference_monthly_price"] == 19.71


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


async def test_finding_discloses_cost_basis_estimate_and_divergence():
    # A finding grounded on a run-rate / representative basis must disclose that it's an estimate, and
    # surface the historical average vs current run-rate when they materially diverge.
    basis = {VM_RID.lower(): {
        "cost_basis": "representative", "cost_is_estimate": True, "cost_variability": "high",
        "cost_material_divergence": True, "historical_monthly": 18.0, "current_run_rate": 80.0,
    }}
    engine = FindingsEngine(pricing=FakePricing(), cost_map={VM_RID.lower(): 600.0}, cost_basis=basis)
    f = (await engine.detect_vm_utilisation_findings([_vm(max_cpu=15.0, peak_memory=20.0)]))[0]
    assert f["details"]["cost_is_estimate"] is True
    assert f["details"]["cost_variability"] == "high"
    assert f["details"]["historical_monthly"] == 18.0 and f["details"]["current_run_rate"] == 80.0


async def test_ahb_flags_anomalously_low_billed_cost():
    # When the billed cost is far below the VM's licence-free (Linux) list price — the CRA-VM case:
    # ₹239 billed vs a ₹13,230 Linux rate — the figure is untrustworthy (sponsored sub or currency/scale
    # issue). AHB still shows the grounded number but flags the anomaly loudly.
    vm = _vm(max_cpu=40.0, sku="Standard_D16s_v3", rid="/s/win"); vm["name"] = "win"
    engine = FindingsEngine(pricing=_AHB_PRICING, cost_map={"/s/win": 5.0})  # 5 vs Linux 560.64 → <10%
    f = (await engine.detect_windows_ahb([vm]))[0]
    assert f["details"]["cost_anomaly"] is True
    assert f["details"]["anomalous_low_count"] == 1
    assert "far below their size" in f["description"]


async def test_ahb_no_anomaly_when_cost_is_reasonable():
    vm = _vm(max_cpu=40.0, sku="Standard_D16s_v3", rid="/s/win"); vm["name"] = "win"
    engine = FindingsEngine(pricing=_AHB_PRICING, cost_map={"/s/win": 300.0})  # 300 vs Linux 560 → 53%
    f = (await engine.detect_windows_ahb([vm]))[0]
    assert f["details"]["cost_anomaly"] is False


async def test_ahb_discloses_estimate_when_grounded_on_run_rate():
    # AHB on a partial/variable resource must flag cost_is_estimate so the UI shows the estimate chip.
    vm = _vm(max_cpu=40.0, sku="Standard_D16s_v3", rid="/s/win"); vm["name"] = "win"
    basis = {"/s/win": {"cost_basis": "run_rate", "cost_is_estimate": True, "cost_variability": "high"}}
    engine = FindingsEngine(pricing=_AHB_PRICING, cost_map={"/s/win": 300.0}, cost_basis=basis)
    f = (await engine.detect_windows_ahb([vm]))[0]
    assert f["details"]["cost_is_estimate"] is True
    assert f["details"]["cost_variability"] == "high"


async def test_no_finding_ever_exceeds_measured_spend():
    # ABSOLUTE GUARANTEE (last-resort net): even a grounded finding is clamped so it can never exceed the
    # subscription's total measured monthly spend — savings > spend must never be shown.
    engine = FindingsEngine(
        pricing=FakePricing(),
        cost_map={VM_RID.lower(): 100.0},  # the VM's actual cost exceeds the tiny measured spend
        measured_monthly_spend=5.0,
    )
    f = (await engine.detect_vm_utilisation_findings([_vm(max_cpu=3.0, peak_memory=6.0)]))[0]
    assert f["category"] == "idle_vms"
    assert f["estimated_savings_monthly"] == 5.0            # grounded 100 clamped to measured spend 5
    assert f["details"]["savings_capped_at_measured_spend"] is True


# ── Commitments: Reserved Instances (authoritative — Azure reservation engine only) ──
# VM RIs (and every non-VM RI) come SOLELY from Azure's Consumption reservationRecommendations, parsed
# into groups and turned into findings by commitments_from_recommendations. The old retail-estimate VM
# detector (detect_vm_commitments) was retired: the Retail Prices API doesn't publish reservation
# prices for most VM SKUs, so it either fabricated a discount or under-covered. There is no longer any
# tool-computed RI discount, prod/nonprod guess, or retail RI fallback anywhere in the engine.

def _vm_ri_group(sku="Standard_D2s_v3", p1=100.0, p3=160.0, qty=3, region="eastus",
                 subscription_id="sub-1", scope="Single"):
    """A parsed VM reservationRecommendation group (what reservations.py produces for virtualmachines)."""
    terms = {}
    if p1 is not None:
        terms["P1Y"] = {"monthly_savings": p1, "monthly_ondemand": 400.0,
                        "monthly_reserved": round(400.0 - p1, 2), "quantity": qty}
    if p3 is not None:
        terms["P3Y"] = {"monthly_savings": p3, "monthly_ondemand": 400.0,
                        "monthly_reserved": round(400.0 - p3, 2), "quantity": qty}
    return {"resource_type": "virtualmachines", "category": "ri_vm", "product": "Virtual Machines",
            "sku": sku, "region": region, "scope": scope, "flexibility_group": None,
            "subscription_id": subscription_id, "terms": terms}


def _current_vm(sku="Standard_D2s_v3", subscription_id="sub-1", region="eastus", name="vm-1", rid=None):
    """A current Resource-Graph VM inventory row (running/deallocated), scoped to the assessed sub."""
    rid = rid or (f"/subscriptions/{subscription_id}/resourceGroups/rg-a/providers/"
                  f"microsoft.compute/virtualmachines/{name}")
    return {"id": rid, "name": name, "subscriptionId": subscription_id, "location": region,
            "vmSize": sku, "powerState": "VM running"}


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


def test_ri_terms_are_independent_and_expose_payg_baseline():
    # 1-year and 3-year savings come from Azure's SEPARATE per-term recommendations and must stay
    # distinct (never one derived from the other); the finding also carries the PAYG on-demand baseline
    # so the drawer can show PAYG vs 1-year vs 3-year cost/savings/%.
    engine = FindingsEngine(pricing=FakePricing())
    d = engine.commitments_from_recommendations([_vm_ri_group(p1=100.0, p3=160.0, qty=3)])[0]["details"]
    assert d["total_1yr_monthly"] == 100.0
    assert d["total_3yr_monthly"] == 160.0
    assert d["total_1yr_monthly"] != d["total_3yr_monthly"]      # not the same price
    assert d["total_ondemand_monthly"] == 400.0                  # Azure's costWithNoReservedInstances


def test_ri_single_term_does_not_fabricate_the_other():
    # Azure returned ONLY a 1-year rec (no 3-year) → the 3-year total is None (not a fabricated copy of
    # the 1-year figure); the headline uses the 1-year saving.
    engine = FindingsEngine(pricing=FakePricing())
    f = engine.commitments_from_recommendations([_vm_ri_group(p1=100.0, p3=None, qty=2)])[0]
    d = f["details"]
    assert d["total_1yr_monthly"] == 100.0
    assert d["total_3yr_monthly"] is None
    assert d["has_1yr"] is True and d["has_3yr"] is False
    assert [o["label"] for o in d["reservation_options"]] == ["1-year Reserved Instance"]  # no fake 3yr
    assert f["estimated_savings_monthly"] == 100.0


def test_ri_three_year_only_does_not_fabricate_a_one_year_value():
    # THE regression guard for the fabrication bug: when Azure returns ONLY a 3-year rec, the 1-year
    # total must be None — NOT a copy of the 3-year saving. (The old aggregate did `one_total += s3`
    # when s1 was absent, so the drawer showed the 3-year figure as if it were an independent 1-year
    # price.) The 3-year figure is used for the headline; the 1-year option is omitted entirely.
    engine = FindingsEngine(pricing=FakePricing())
    f = engine.commitments_from_recommendations([_vm_ri_group(p1=None, p3=160.0, qty=2)])[0]
    d = f["details"]
    assert d["total_3yr_monthly"] == 160.0
    assert d["total_1yr_monthly"] is None              # NOT fabricated from the 3-year value
    assert d["has_3yr"] is True and d["has_1yr"] is False
    # per-item 1-year saving is also None (not a copy of the 3-year saving)
    assert d["reservation_items"][0]["monthly_savings"] is None
    assert d["reservation_items"][0]["monthly_savings_3yr"] == 160.0
    assert [o["label"] for o in d["reservation_options"]] == ["3-year Reserved Instance"]  # no fake 1yr
    assert f["estimated_savings_monthly"] == 160.0


def test_ri_mixed_terms_sum_each_term_purely():
    # Two SKUs: one has BOTH terms, one has ONLY 3-year. Per-term totals must sum only the real recs —
    # 1-year total = the single 1-year rec; 3-year total = both 3-year recs. No cross-copying inflates
    # the 1-year figure with the 3-year-only SKU's saving.
    engine = FindingsEngine(pricing=FakePricing())
    groups = [
        _vm_ri_group(sku="Standard_D2s_v3", p1=100.0, p3=160.0, qty=3),
        _vm_ri_group(sku="Standard_E4s_v3", p1=None, p3=90.0, qty=1),
    ]
    d = engine.commitments_from_recommendations(groups)[0]["details"]
    assert d["total_1yr_monthly"] == 100.0             # only the SKU with a real 1-year rec
    assert d["total_3yr_monthly"] == 250.0             # 160 + 90 (both real 3-year recs)
    assert d["n_1yr"] == 1 and d["n_3yr"] == 2


def test_vm_with_no_azure_ri_recommendation_produces_nothing():
    # Azure returned no VM reservation recommendation → we recommend no RI (never fabricate one),
    # even for a busy, steadily-running VM.
    engine = FindingsEngine(pricing=FakePricing())
    assert engine.commitments_from_recommendations([]) == []


# ── RI reconciliation against CURRENT subscription inventory ────────────────────────
# Azure's reservation recommendations are historical/usage-based and can name SKUs for VMs that have
# been moved out of (or deleted from) the assessed subscription. The reconciliation layer treats the
# current Resource-Graph VM inventory as the hard boundary before an RI becomes an actionable finding.

def _ri(engine, groups, current_vms):
    """Reconcile then aggregate — the exact production order (assessment.py)."""
    reconciled = reconcile_vm_recommendations(groups, current_vms, assessment_id=1)
    return engine.commitments_from_recommendations(reconciled)


def test_ri_present_vms_produce_finding_with_affected_from_inventory():
    # (Case 1 / 6 / 11) Subscription HAS matching VMs → RI finding is created and its affected-resource
    # list/count comes from CURRENT inventory (real resource ids), not the historical SKU groups.
    engine = FindingsEngine(pricing=FakePricing())
    inv = [_current_vm(name=f"vm-{i}") for i in range(6)]        # 6 current D2s_v3 VMs in sub-1
    out = _ri(engine, [_vm_ri_group(sku="Standard_D2s_v3")], inv)
    assert len(out) == 1
    d = out[0]["details"]
    assert d["affected_count"] == 6                               # equals current matching VM count
    assert len(d["affected_vms"]) == 6
    assert all(vm["id"].startswith("/subscriptions/sub-1/") for vm in d["affected_vms"])


def test_ri_zero_current_vms_produces_no_finding():
    # (Case 2 / 12) THE reported bug: subscription has 0 VMs but Azure returns a historical RI for 6.
    # No actionable RI finding may be created — historical usage is not proof the VMs currently exist.
    engine = FindingsEngine(pricing=FakePricing())
    out = _ri(engine, [_vm_ri_group(sku="Standard_D2s_v5", qty=6)], current_vms=[])
    assert out == []


def test_ri_moved_out_vms_do_not_appear_as_current():
    # (Case 3 / 9) VMs that previously lived in sub-1 were moved to sub-2. A stale sub-1 recommendation
    # must not resurface them: sub-1 inventory no longer contains the SKU → excluded.
    engine = FindingsEngine(pricing=FakePricing())
    inv_sub2_only = [_current_vm(subscription_id="sub-2", name="vm-moved")]
    out = _ri(engine, [_vm_ri_group(sku="Standard_D2s_v3", subscription_id="sub-1")], inv_sub2_only)
    assert out == []


def test_ri_multi_subscription_isolation():
    # (Case 4) sub-A has matching VMs, sub-B does not. Only sub-A's recommendation survives, and every
    # affected resource resolves to sub-A — sub-B resources never leak into the finding.
    engine = FindingsEngine(pricing=FakePricing())
    groups = [
        _vm_ri_group(sku="Standard_D2s_v3", subscription_id="sub-A"),
        _vm_ri_group(sku="Standard_D2s_v3", subscription_id="sub-B"),
    ]
    inv = [_current_vm(subscription_id="sub-A", name="a1"),
           _current_vm(subscription_id="sub-A", name="a2")]   # nothing in sub-B
    out = _ri(engine, groups, inv)
    assert len(out) == 1
    d = out[0]["details"]
    assert d["affected_count"] == 2
    assert {vm["subscription_id"] for vm in d["affected_vms"]} == {"sub-A"}


def test_ri_sku_level_no_matching_vm_creates_no_affected_resources():
    # (Case 5) A SKU-level recommendation whose SKU is absent from current inventory (the sub has OTHER
    # VMs, just not this family) → excluded; no affected VM resources are fabricated.
    engine = FindingsEngine(pricing=FakePricing())
    inv_other_family = [_current_vm(sku="Standard_E4s_v5", name="e1")]  # E-series, not the D2s_v5 rec
    out = _ri(engine, [_vm_ri_group(sku="Standard_D2s_v5")], inv_other_family)
    assert out == []


def test_ri_instance_size_flexibility_is_matched():
    # A rec for Standard_D2s_v5 is covered by a present Standard_D4s_v5 (same flexibility family) — we
    # must NOT false-exclude a present VM of a different size in the recommended family.
    engine = FindingsEngine(pricing=FakePricing())
    inv = [_current_vm(sku="Standard_D4s_v5", name="d4")]
    out = _ri(engine, [_vm_ri_group(sku="Standard_D2s_v5")], inv)
    assert len(out) == 1
    assert out[0]["details"]["affected_count"] == 1


def test_ri_shared_scope_not_auto_single_subscription():
    # (Case 7) A shared-scope (billing-account) recommendation is NOT automatically actionable for the
    # subscription: it only survives if the assessed subscription currently has a matching VM. Here the
    # SKU is absent → excluded, so a shared-scope rec cannot masquerade as a single-sub finding.
    engine = FindingsEngine(pricing=FakePricing())
    out = _ri(engine, [_vm_ri_group(sku="Standard_D2s_v5", scope="Shared")], current_vms=[])
    assert out == []


def test_ri_non_vm_recommendation_passes_through_unreconciled():
    # Non-VM RI (e.g. SQL) has no comparable VM inventory here, so it is NOT dropped by VM reconciliation
    # (documented pass-through) — the fix must not silently kill non-VM reservations.
    engine = FindingsEngine(pricing=FakePricing())
    sql_group = {"resource_type": "sqldatabases", "category": "sql_db_reserved_capacity",
                 "product": "SQL Database", "sku": "GP_Gen5", "region": "eastus", "scope": "Single",
                 "flexibility_group": None, "subscription_id": "sub-1",
                 "terms": {"P1Y": {"monthly_savings": 50.0, "monthly_ondemand": 200.0,
                                   "monthly_reserved": 150.0, "quantity": 1}}}
    out = _ri(engine, [sql_group], current_vms=[])   # zero VMs must not affect a SQL RI
    assert len(out) == 1
    assert out[0]["category"] == "sql_db_reserved_capacity"


def test_ri_stale_removal_zeroes_savings_consistently():
    # (Case 10) When the only RI is stale, it contributes nothing to savings — there is simply no RI
    # finding, so category/total savings and the affected-resource count are all consistently zero.
    engine = FindingsEngine(pricing=FakePricing())
    out = _ri(engine, [_vm_ri_group(sku="Standard_D2s_v5", p1=100.0, p3=160.0)], current_vms=[])
    assert out == []
    assert sum(f["estimated_savings_monthly"] for f in out) == 0


# ── Azure SQL Database reservation recommendations (vCore eligibility + reconciliation) ──
# SQL Database reservation pricing applies ONLY to the vCore purchasing model; DTU databases are not
# eligible. Azure's recommendation is SKU/usage-based (no database id), so it is reconciled against the
# current SQL DB inventory exactly like VMs, but with vCore-vs-DTU eligibility.

def _sql_ri_group(sku="SQLDB_Gen5", p1=40.0, p3=60.0, qty=2, region="eastus",
                  subscription_id="sub-1", scope="Single"):
    """A parsed SQL Database reservationRecommendation group (category sql_db_reserved_capacity)."""
    terms = {}
    if p1 is not None:
        terms["P1Y"] = {"monthly_savings": p1, "monthly_ondemand": 200.0,
                        "monthly_reserved": round(200.0 - p1, 2), "quantity": qty}
    if p3 is not None:
        terms["P3Y"] = {"monthly_savings": p3, "monthly_ondemand": 200.0,
                        "monthly_reserved": round(200.0 - p3, 2), "quantity": qty}
    return {"resource_type": "sqldatabases", "category": "sql_db_reserved_capacity",
            "product": "SQL Database", "sku": sku, "region": region, "scope": scope,
            "flexibility_group": None, "subscription_id": subscription_id, "terms": terms}


def _current_sql_db(tier="GeneralPurpose", subscription_id="sub-1", region="eastus",
                    name="db-1", sku="GP_Gen5_2", rid=None):
    """A current Resource-Graph SQL Database inventory row (vCore tiers = GeneralPurpose/BusinessCritical/
    Hyperscale; DTU tiers = Basic/Standard/Premium)."""
    rid = rid or (f"/subscriptions/{subscription_id}/resourceGroups/rg/providers/"
                  f"microsoft.sql/servers/srv/databases/{name}")
    return {"id": rid, "name": name, "subscriptionId": subscription_id, "location": region,
            "tier": tier, "skuName": sku, "capacity": 2}


def _sql_ri(engine, groups, sql_dbs):
    reconciled = reconcile_sql_recommendations(groups, sql_dbs, assessment_id=7)
    return engine.commitments_from_recommendations(reconciled)


def test_sql_reservation_resourcetype_and_sku_are_parsed_not_dropped():
    # (Investigation #3/#7/#9/#12) The Consumption API's resourceType 'SQLDatabases' maps to the SQL
    # reserved-capacity category, and the SKU is read from skuName (SQL recs have no normalizedSize) — so
    # a real SQL reservation is neither dropped for a missing SKU nor misclassified as VM/Other.
    raw = [{"properties": {"resourceType": "SQLDatabases", "skuName": "SQLDB_Gen5", "location": "eastus",
            "term": "P3Y", "scope": "Single", "lookBackPeriod": "Last30Days", "recommendedQuantity": 2,
            "subscriptionId": "sub-1", "netSavings": 60.0, "costWithNoReservedInstances": 200.0,
            "totalCostWithReservedInstances": 140.0}}]
    groups = parse_reservation_recommendations(raw, "sub-1")
    assert len(groups) == 1
    assert groups[0]["category"] == "sql_db_reserved_capacity"   # NOT ri_vm, NOT dropped
    assert groups[0]["sku"] == "SQLDB_Gen5"                       # extracted from skuName


def test_sql_purchasing_model_classification():
    assert sql_purchasing_model({"tier": "GeneralPurpose"}) == "vCore"
    assert sql_purchasing_model({"tier": "BusinessCritical"}) == "vCore"
    assert sql_purchasing_model({"tier": "Hyperscale"}) == "vCore"
    assert sql_purchasing_model({"tier": "Standard"}) == "DTU"
    assert sql_purchasing_model({"tier": "Basic"}) == "DTU"


def test_sql_vcore_db_plus_recommendation_creates_finding():
    # (Case 13 / 16 / 19 / 20) Current vCore SQL DB + Azure SQL reservation rec → a SQL Reserved Capacity
    # finding whose affected resources are the ACTUAL database resource ids (never the subscription id).
    engine = FindingsEngine(pricing=FakePricing())
    out = _sql_ri(engine, [_sql_ri_group()], [_current_sql_db(tier="GeneralPurpose")])
    assert len(out) == 1
    assert out[0]["category"] == "sql_db_reserved_capacity"      # correct category, not "Other"/advisor
    d = out[0]["details"]
    assert d["affected_count"] == 1
    assert d["affected_vms"][0]["id"].endswith("/databases/db-1")   # real DB id
    assert d["affected_vms"][0]["id"] != "sub-1"                    # never the subscription id


def test_sql_dtu_db_is_not_reservation_eligible():
    # (Cases E + F) Neither a DTU Basic nor a DTU Standard database is vCore, so even if a recommendation
    # exists it is not actionable — no SQL RI finding is created for either.
    engine = FindingsEngine(pricing=FakePricing())
    for dtu_tier in ("Basic", "Standard"):
        out = _sql_ri(engine, [_sql_ri_group()], [_current_sql_db(tier=dtu_tier)])
        assert out == [], f"DTU {dtu_tier} must not produce a SQL RI finding"


def test_sql_db_present_but_no_recommendation_fabricates_nothing():
    # (Case 15) A current vCore SQL DB with NO Azure recommendation → no fabricated finding or savings.
    engine = FindingsEngine(pricing=FakePricing())
    out = _sql_ri(engine, [], [_current_sql_db(tier="GeneralPurpose")])
    assert out == []


def test_sql_recommendation_with_no_matching_current_db_is_excluded():
    # (Case 17) Azure returns a SQL reservation rec but the subscription has no current SQL DB (moved out
    # / deleted) → stale/unverifiable, excluded from actionable savings.
    engine = FindingsEngine(pricing=FakePricing())
    out = _sql_ri(engine, [_sql_ri_group()], [])
    assert out == []


def test_sql_recommendation_never_becomes_vm_or_other_and_survives_vm_reconciler():
    # (Case 18 / 19) The VM reconciler must pass SQL groups through untouched (zero VMs must not drop a
    # SQL rec), and the category must remain SQL reserved capacity.
    engine = FindingsEngine(pricing=FakePricing())
    reconciled = reconcile_vm_recommendations([_sql_ri_group()], current_vms=[], assessment_id=7)
    assert len(reconciled) == 1 and reconciled[0]["category"] == "sql_db_reserved_capacity"
    out = _sql_ri(engine, reconciled, [_current_sql_db()])
    assert out and out[0]["category"] == "sql_db_reserved_capacity"


def test_sql_reservation_preserves_billing_currency_no_conversion():
    # (Case 22) Savings use Azure's figures verbatim in the assessment's billing currency — no USD
    # hardcoding, no conversion (INR in → INR value out, symbol ₹ in the client-facing text).
    engine = FindingsEngine(pricing=FakePricing(), currency="INR")
    out = _sql_ri(engine, [_sql_ri_group(p1=40.0, p3=60.0)], [_current_sql_db()])
    assert out[0]["estimated_savings_monthly"] == 60.0          # Azure's 3-year net saving, unchanged
    assert "₹" in out[0]["description"]                          # billing currency symbol, not $


def test_sql_reservation_multi_subscription_isolation():
    # (Case 21 / 23 / D) sub-A has a vCore SQL DB, sub-B does not → only sub-A's rec survives and every
    # affected DB resolves to sub-A; sub-B never contaminates the finding.
    engine = FindingsEngine(pricing=FakePricing())
    groups = [_sql_ri_group(subscription_id="sub-A"), _sql_ri_group(subscription_id="sub-B")]
    dbs = [_current_sql_db(subscription_id="sub-A", name="a-db")]     # nothing in sub-B
    out = _sql_ri(engine, groups, dbs)
    assert len(out) == 1
    d = out[0]["details"]
    assert d["affected_count"] == 1
    assert {r["subscription_id"] for r in d["affected_vms"]} == {"sub-A"}


def test_sql_ri_affected_is_a_database_never_server_or_subscription():
    # (Case E / F) The affected resource is always the ACTUAL database resource id (contains
    # '/databases/') — never the subscription id, never a bare server id.
    engine = FindingsEngine(pricing=FakePricing())
    out = _sql_ri(engine, [_sql_ri_group(subscription_id="sub-1")], [_current_sql_db(name="db-x")])
    rid = out[0]["details"]["affected_vms"][0]["id"]
    assert "/databases/" in rid                       # a database, resolved from inventory
    assert rid != "sub-1" and not rid.endswith("/servers/srv")   # not the subscription, not the server


def test_sql_ri_region_mismatch_is_relaxed_when_eligible_vcore_exists():
    # THE likely live cause: Azure's recommendation region string and the ARG `location` string diverge.
    # With eligible vCore SQL DBs present, the finding must still surface (region relaxed) rather than
    # being false-excluded — resolved against the current DB.
    engine = FindingsEngine(pricing=FakePricing())
    rec = _sql_ri_group(region="West US")                          # Azure display-name style
    db = _current_sql_db(region="westus")                          # ARG canonical style (won't string-eq)
    out = _sql_ri(engine, [rec], [db])
    assert len(out) == 1
    assert out[0]["details"]["affected_count"] == 1


def test_sql_ri_region_mismatch_still_excluded_when_no_eligible_vcore():
    # Region relaxation must NOT fabricate: if there is no eligible vCore SQL DB in the subscription at
    # all, the recommendation is still excluded (no finding).
    engine = FindingsEngine(pricing=FakePricing())
    out = _sql_ri(engine, [_sql_ri_group(region="West US")], [_current_sql_db(region="eastus", tier="Standard")])
    assert out == []


def test_sql_vcore_inferred_from_skuname_when_tier_blank():
    # (classification robustness) When the inventory row's sku.tier is blank, the vCore model is inferred
    # from the SKU name (GP_/BC_/HS_/Gen family) so a real vCore DB isn't dropped as ineligible.
    assert sql_purchasing_model({"tier": "", "skuName": "GP_Gen5_2"}) == "vCore"
    assert sql_purchasing_model({"tier": "", "skuName": "BC_Gen5_8"}) == "vCore"
    assert sql_purchasing_model({"tier": "", "skuName": "S3"}) == "DTU"
    engine = FindingsEngine(pricing=FakePricing())
    out = _sql_ri(engine, [_sql_ri_group()], [_current_sql_db(tier="", sku="GP_Gen5_2", name="db-b")])
    assert len(out) == 1 and out[0]["details"]["affected_count"] == 1


def test_sql_ri_terms_independent_1yr_and_3yr():
    # (Case G) 1-year and 3-year SQL savings are kept independent; a single-term rec does not fabricate
    # the other term.
    engine = FindingsEngine(pricing=FakePricing())
    both = _sql_ri(engine, [_sql_ri_group(p1=40.0, p3=60.0)], [_current_sql_db()])[0]["details"]
    assert both["total_1yr_monthly"] == 40.0 and both["total_3yr_monthly"] == 60.0
    assert both["total_1yr_monthly"] != both["total_3yr_monthly"]
    only3 = _sql_ri(engine, [_sql_ri_group(p1=None, p3=60.0)], [_current_sql_db()])[0]["details"]
    assert only3["total_3yr_monthly"] == 60.0 and only3["total_1yr_monthly"] is None


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


def test_deallocated_vms_aggregate_into_one_finding():
    # Multiple deallocated VMs → ONE aggregated finding (not one card per VM), with the VMs listed in
    # details and the total = sum of every VM's still-billing disk cost.
    def _vm(i, cost):
        did = f"/s/disk-{i}"
        return ({"id": f"/s/vm-{i}", "name": f"vm-{i}", "subscriptionId": "s", "location": "eastus",
                 "vmSize": "Standard_D2s_v3", "powerState": "VM deallocated",
                 "osDiskId": did, "dataDisks": []}, {did.lower(): cost})
    vms, cost_map = [], {}
    for i, c in enumerate([10.0, 20.0, 5.0]):
        vm, cm = _vm(i, c)
        vms.append(vm)
        cost_map.update(cm)
    out = FindingsEngine(pricing=FakePricing(), cost_map=cost_map).detect_deallocated_vms(vms)
    assert len(out) == 1                                    # ONE aggregated card, not three
    f = out[0]
    assert f["resource_id"] is None                         # resource-less aggregate (escapes dedupe)
    assert f["estimated_savings_monthly"] == 35.0           # 10 + 20 + 5
    assert f["details"]["affected_count"] == 3
    assert len(f["details"]["affected_vms"]) == 3
    assert f["details"]["affected_vms"][0]["monthly_savings"] == 20.0   # sorted by saving desc


def test_deallocated_vm_without_cost_data_is_dropped():
    # No per-resource billing → can't quantify the disk cost → the VM is excluded (no fabricated finding).
    vm = {"id": "/s/vm", "name": "vm", "subscriptionId": "s", "vmSize": "Standard_D2s_v3",
          "powerState": "VM deallocated", "osDiskId": "/s/disk", "dataDisks": []}
    assert FindingsEngine(pricing=FakePricing()).detect_deallocated_vms([vm]) == []


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


async def test_empty_load_balancer_without_billed_cost_is_review():
    # No actual billed cost → REVIEW: the live retail rate is a reference only, never a saving.
    engine = FindingsEngine(pricing=FakePricing())
    f = (await engine.detect_orphans("empty_load_balancers", [_row("/s/lb-1", skuName="Standard")]))[0]
    assert f["category"] == "empty_load_balancers"
    assert f["evidence_state"] == "review"
    assert f["estimated_savings_monthly"] == 0.0
    assert f["details"]["reference_monthly_price"] == 18.0  # live Retail Prices rate, reference only


async def test_idle_nat_gateway_without_billed_cost_is_review():
    engine = FindingsEngine(pricing=FakePricing())
    f = (await engine.detect_orphans("idle_nat_gateways", [_row("/s/nat-1")]))[0]
    assert f["category"] == "idle_nat_gateways"
    assert f["evidence_state"] == "review"
    assert f["estimated_savings_monthly"] == 0.0
    assert f["details"]["reference_monthly_price"] == 32.0  # live Retail Prices rate, reference only


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


# ── SECOND RI leak path: Azure Advisor reservation-purchase recommendations ─────────
# The live #118 bug: an Advisor VM reservation rec is subscription-scoped (resource = the subscription
# itself) and bypassed the Consumption reconciliation, leaking into "Other" with the subscription id as
# the affected resource — even with zero current VMs.

def _advisor_vm_reservation_rec(sub_id="005e9433-0d52-4b38-97aa-1b2c3d4e5f60", rec_id="ADV-RI"):
    """An Azure Advisor VM reservation PURCHASE recommendation — subscription-scoped, exactly the shape
    of the live leak ("Consider virtual machine reserved instance to save over the on-demand costs")."""
    return {
        "id": f"/subscriptions/{sub_id}/providers/Microsoft.Advisor/recommendations/{rec_id}",
        "properties": {
            "category": "Cost", "impact": "Medium",
            "impactedField": "Microsoft.Subscriptions/subscriptions", "impactedValue": sub_id,
            "shortDescription": {
                "problem": "Consider virtual machine reserved instance to save over the on-demand costs",
                "solution": "Buy a VM reserved instance"},
            "extendedProperties": {"annualSavingsAmount": "396", "savingsAmount": "33"},
            "resourceMetadata": {"resourceId": f"/subscriptions/{sub_id}"},
        },
    }


def test_advisor_scope_detection_helper():
    # A real deployed resource id contains '/providers/'; anything shallower is subscription/scope.
    assert advisor_rec_is_subscription_scoped("/subscriptions/abc") is True
    assert advisor_rec_is_subscription_scoped("005e9433-0d52-4b38-97aa-1b2c3d4e5f60") is True  # bare guid
    assert advisor_rec_is_subscription_scoped("") is True
    assert advisor_rec_is_subscription_scoped(VM_RID) is False               # real VM resource
    assert advisor_rec_is_subscription_scoped(DISK_RID) is False             # real disk resource
    # impactedField can flag a reservation even if a resourceId is present
    assert advisor_rec_is_subscription_scoped(
        VM_RID, impacted_field="Microsoft.Subscriptions/subscriptions") is True


def test_advisor_vm_reservation_rec_never_becomes_a_finding():
    # (Live #118) The Advisor VM reservation rec must NOT become a finding — reservations come only from
    # the inventory-reconciled Consumption path — and must NEVER use the subscription id as a resource.
    engine = FindingsEngine(pricing=FakePricing())
    out = engine.advisor_findings([_advisor_vm_reservation_rec()])
    assert out == []


def test_advisor_reservation_rec_excluded_even_mixed_with_a_real_rec():
    # Only the subscription-scoped reservation rec is dropped; a genuine resource-scoped rec survives and
    # its resource is a REAL resource id (never the subscription).
    engine = FindingsEngine(pricing=FakePricing(),
                            advisor_index=build_advisor_index([_advisor_rec(DISK_RID)]))
    out = engine.advisor_findings([_advisor_vm_reservation_rec(), _advisor_rec(DISK_RID)])
    assert len(out) == 1
    assert "/providers/" in (out[0]["resource_id"] or "")     # real resource, not the subscription id
    assert out[0]["advisor_recommendation_id"] == "ADV-1"


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


async def test_orphan_saving_is_grounded_in_actual_billed_cost():
    # An orphan's saving is its ACTUAL billed cost — never the retail list price. Disk actually costs $5
    # (retail list is 19.71), so the saving IS $5 and reads as validated (grounded, not a list guess).
    engine = FindingsEngine(pricing=FakePricing(), cost_map={DISK_RID.lower(): 5.0})
    f = (await engine.detect_unattached_disks([_disk()]))[0]
    assert f["estimated_savings_monthly"] == 5.0            # = actual billed cost (not the 19.71 list rate)
    assert f["validation_status"] == "validated"
    assert f["evidence_state"] == "quantified"
    assert f["details"]["savings_source"] == "ACTUAL_BILLED_COST"


async def test_validation_validated_within_tolerance():
    engine = FindingsEngine(pricing=FakePricing(), cost_map={DISK_RID.lower(): 19.0})
    f = (await engine.detect_unattached_disks([_disk()]))[0]
    assert f["validation_status"] == "validated"
    assert f["actual_monthly_cost"] == 19.0


async def test_orphan_saving_equals_actual_even_above_retail():
    # The orphan saving tracks ACTUAL billed cost, whatever the retail list rate is. Disk actually costs
    # $30/mo (retail list 19.71) → the saving is the real $30 you stop paying, grounded and validated.
    engine = FindingsEngine(pricing=FakePricing(), cost_map={DISK_RID.lower(): 30.0})
    f = (await engine.detect_unattached_disks([_disk()]))[0]
    assert f["estimated_savings_monthly"] == 30.0
    assert f["estimated_savings_annual"] == 360.0
    assert f["evidence_state"] == "quantified"


# ── Output shape matches the DB model ────────────────────────────────────────────

async def test_finding_keys_match_model_columns():
    engine = FindingsEngine(pricing=FakePricing())
    f = (await engine.detect_unattached_disks([_disk()]))[0]
    assert set(f.keys()) <= FINDING_COLUMNS, set(f.keys()) - FINDING_COLUMNS
