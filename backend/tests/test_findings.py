"""Tests for the findings engine (Step 6)."""
from __future__ import annotations

from app.models.db import Finding
from app.services import assessment as pipeline
from app.services import estimates
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


class FakePricing:
    """Deterministic stand-in for PricingEngine (no network). VM price is per-SKU so the engine's
    'price current vs price target' downsize math can be tested against real deltas."""
    def __init__(self, disk=19.71, pip=3.65, vm_prices=None, ri=None, sp=None, win=None, currency="USD"):
        self.disk = disk
        self.pip = pip
        self.vm_prices = dict(DEFAULT_VM_PRICES if vm_prices is None else vm_prices)
        self.ri = dict(DEFAULT_RI if ri is None else ri)
        self.sp = dict(DEFAULT_SP if sp is None else sp)
        self.win = dict(DEFAULT_WIN if win is None else win)
        self.currency = currency  # flat-rate methods return in this currency (like the real engine)

    async def get_managed_disk_monthly_price(self, region, sku, size):
        return self.disk

    async def get_public_ip_monthly_price(self, region, sku="Standard"):
        return self.pip

    async def get_load_balancer_monthly_price(self, region):
        return from_usd(estimates.LOAD_BALANCER_MONTHLY_USD, self.currency)

    async def get_nat_gateway_monthly_price(self, region):
        return from_usd(estimates.NAT_GATEWAY_MONTHLY_USD, self.currency)

    async def get_bastion_monthly_price(self, region, sku="Basic"):
        return from_usd(estimates.BASTION_MONTHLY_USD, self.currency)

    async def get_app_service_plan_monthly_price(self, region, sku):
        return from_usd(estimates.APP_SERVICE_PLAN_MONTHLY_USD.get((sku or "").lower(), 0.0), self.currency)

    async def get_vm_monthly_price(self, region, sku):
        return self.vm_prices.get(sku)

    async def get_vm_reserved_monthly_price(self, region, sku, term="1 Year"):
        return self.ri.get((sku, term))

    async def get_vm_savings_plan_monthly_price(self, region, sku, term="1 Year"):
        return self.sp.get((sku, term))

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
    assert severity_from_savings(5, advisor_impact="High") == "high"  # impact raises


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
    # Peak CPU 3%, peak memory 6% — both under the idle bars → idle → full-cost saving.
    engine = FindingsEngine(pricing=FakePricing())
    f = (await engine.detect_vm_utilisation_findings([_vm(max_cpu=3.0, peak_memory=6.0)]))[0]
    assert f["category"] == "idle_vms"
    assert f["estimated_savings_monthly"] == 560.64  # full D16s_v3 price


async def test_spiky_cpu_vm_not_flagged_idle_or_downsized():
    # Real HyperV-Demo case: peak CPU 44% on a D16s_v3. Even D8 can't absorb it (44%*16/8=88%>70%)
    # → no candidate fits → well-utilised, left alone. This is the exact scenario that was
    # wrongly flagged "idle → delete" under the old average-CPU-only logic.
    engine = FindingsEngine(pricing=FakePricing())
    findings = await engine.detect_vm_utilisation_findings(
        [_vm(max_cpu=44.0, peak_memory=30.0, avg_cpu=1.2)]
    )
    assert findings == []


async def test_high_memory_low_cpu_not_flagged_idle():
    # The "Redis cache" case: peak CPU only 2% (would look idle alone) but peak memory 85% —
    # real work is happening in memory. Must NOT be idle, and must NOT be downsizable either
    # (memory doesn't fit even on the next size down: 85%*64/32 = 170%).
    engine = FindingsEngine(pricing=FakePricing())
    findings = await engine.detect_vm_utilisation_findings(
        [_vm(max_cpu=2.0, peak_memory=85.0)]
    )
    assert findings == []  # not idle (memory too high), not downsizable (no candidate fits)


async def test_downsize_recommends_real_target_sku_and_real_price_delta():
    # Peak CPU 15% (2.4 cores of 16), peak memory 20% (12.8 GB of 64) on a D16s_v3.
    # D8s_v3 fits both (30% CPU, 40% memory); D4s_v3 doesn't (60% CPU, 80% memory) → target = D8s_v3.
    engine = FindingsEngine(pricing=FakePricing())
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
    engine = FindingsEngine(pricing=FakePricing())
    findings = await engine.detect_vm_utilisation_findings(
        [_vm(max_cpu=55.0, peak_memory=60.0)]
    )
    assert findings == []


async def test_memory_unavailable_still_allows_cpu_only_downsize_but_lower_confidence():
    # CPU very low (2%) but memory could NOT be measured. Must NOT be treated as idle (that was
    # exactly the earlier bug) — instead a conservative CPU-only downsize using the ladder, at
    # reduced confidence, with the caveat recorded.
    engine = FindingsEngine(pricing=FakePricing())
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
    engine = FindingsEngine(pricing=FakePricing(vm_prices=prices))
    findings = await engine.detect_vm_utilisation_findings(
        [_vm(max_cpu=15.0, peak_memory=20.0)]
    )
    assert findings == []


async def test_vm_without_cpu_metrics_is_skipped():
    engine = FindingsEngine(pricing=FakePricing())
    findings = await engine.detect_vm_utilisation_findings([_vm(max_cpu=None, datapoints=0)])
    assert findings == []


# ── Commitments: Reserved Instances + Savings Plan ───────────────────────────────

def _steady_vm(max_cpu=50.0, peak_memory=40.0, datapoints=30, rid=VM_RID):
    # Well-utilised (not idle, not downsizable) + full metric coverage → steady.
    return _vm(max_cpu=max_cpu, peak_memory=peak_memory, datapoints=datapoints, rid=rid)


async def test_vm_commitment_recommends_ri_with_all_options():
    engine = FindingsEngine(pricing=FakePricing())  # D16: payg 560.64, ri1 400, ri3 350
    findings = await engine.detect_vm_commitments([_steady_vm()])
    ri = [f for f in findings if f["category"] == "ri_vm"][0]
    assert ri["resource_id"] is None                       # aggregated → resource-less
    assert ri["estimated_savings_monthly"] == round(560.64 - 350, 2)  # best case 3yr = 210.64
    labels = [o["label"] for o in ri["details"]["reservation_options"]]
    assert labels == ["3-year Reserved Instance", "1-year Reserved Instance"]  # best case first
    assert ri["details"]["total_1yr_monthly"] == round(560.64 - 400, 2)  # 160.64
    assert ri["details"]["total_3yr_monthly"] == round(560.64 - 350, 2)  # 210.64
    assert ri["details"]["item_count"] == 1
    assert ri["details"]["reservation_items"][0]["sku"] == "Standard_D16s_v3"
    assert ri["confidence"] >= 0.8


async def test_commitment_skips_idle_vm():
    engine = FindingsEngine(pricing=FakePricing())
    # Idle (both signals low) → reserve makes no sense; idle detector handles it.
    assert await engine.detect_vm_commitments([_vm(max_cpu=2.0, peak_memory=3.0)]) == []


async def test_commitment_advisor_wins():
    recs = [_advisor_rec(VM_RID)]
    engine = FindingsEngine(pricing=FakePricing(), advisor_index=build_advisor_index(recs))
    assert await engine.detect_vm_commitments([_steady_vm()]) == []


async def test_commitment_measured_basis_skips_unconfirmed():
    engine = FindingsEngine(pricing=FakePricing(), reservation_basis="measured")
    # Few datapoints → usage unconfirmed → measured basis skips it.
    assert await engine.detect_vm_commitments([_steady_vm(datapoints=5)]) == []


async def test_commitment_combined_unconfirmed_lower_confidence():
    engine = FindingsEngine(pricing=FakePricing(), reservation_basis="combined")
    f = (await engine.detect_vm_commitments([_steady_vm(datapoints=5)]))[0]
    assert f["category"] == "ri_vm"
    assert f["confidence"] < 0.7  # always-on fallback (usage unconfirmed) → lower confidence


async def test_commitment_advisor_basis_produces_nothing():
    engine = FindingsEngine(pricing=FakePricing(), reservation_basis="advisor")
    assert await engine.detect_vm_commitments([_steady_vm()]) == []


# ── Commitments from Azure's own reservation engine (authoritative) ──────────────

def _ri_group(category="ri_vm", sku="Standard_D2s_v3", p1=100.0, p3=160.0, qty=2):
    terms = {}
    if p1 is not None:
        terms["P1Y"] = {"monthly_savings": p1, "monthly_ondemand": 400.0,
                        "monthly_reserved": round(400.0 - p1, 2), "quantity": qty}
    if p3 is not None:
        terms["P3Y"] = {"monthly_savings": p3, "monthly_ondemand": 400.0,
                        "monthly_reserved": round(400.0 - p3, 2), "quantity": qty}
    return {"resource_type": "virtualmachines", "category": category, "product": "Virtual Machines",
            "sku": sku, "region": "eastus", "scope": "Single", "flexibility_group": "DSv3 Series",
            "subscription_id": "sub-1", "terms": terms}


def test_recommendations_build_ri_finding_best_case_3yr():
    engine = FindingsEngine(pricing=FakePricing())
    f = engine.commitments_from_recommendations([_ri_group()])[0]
    assert f["category"] == "ri_vm"
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


async def test_commitment_uses_savings_plan_when_ri_unavailable():
    # A SKU with no Reserved Instance offering in retail (ri={}) must NOT get a fabricated RI — it
    # falls back to the Savings Plan Azure actually offers for it.
    engine = FindingsEngine(pricing=FakePricing(ri={}))  # D16: payg 560.64, no RI, SP 1yr 450
    f = (await engine.detect_vm_commitments([_steady_vm()]))[0]
    assert f["category"] == "savings_plan_vm"
    assert f["details"]["kind"] == "Savings Plan"
    assert f["estimated_savings_monthly"] == round(560.64 - 450, 2)  # 110.64 (1yr SP)


async def test_commitment_burstable_falls_back_to_savings_plan():
    # B-series can't take a Reserved Instance → it rolls into the Savings Plan finding, not RI.
    pricing = FakePricing(vm_prices={"Standard_B2ms": 60.0}, ri={},
                          sp={("Standard_B2ms", "1 Year"): 45.0})
    engine = FindingsEngine(pricing=pricing)
    vm = _vm(max_cpu=50.0, peak_memory=40.0, sku="Standard_B2ms")
    findings = await engine.detect_vm_commitments([vm])
    cats = {f["category"] for f in findings}
    assert cats == {"savings_plan_vm"}                    # no RI finding for a burstable VM
    sp = findings[0]
    assert sp["details"]["kind"] == "Savings Plan"
    assert sp["estimated_savings_monthly"] == round(60.0 - 45.0, 2)  # 15.0


# ── Windows Azure Hybrid Benefit (aggregated into one finding) ───────────────────

async def test_windows_ahb_aggregates_eligible_vms():
    # Consistent per-vCore prices (33.58/vCore): D2s_v3 (2) → 67.16, D16s_v3 (16) → 537.28.
    pricing = FakePricing(
        vm_prices={"Standard_D2s_v3": 70.08, "Standard_D16s_v3": 560.64},
        win={"Standard_D2s_v3": 137.24, "Standard_D16s_v3": 1097.92},
    )
    vms = [
        _vm(max_cpu=40.0, sku="Standard_D2s_v3", rid="/s/vm-a"),
        _vm(max_cpu=40.0, sku="Standard_D16s_v3", rid="/s/vm-b"),
    ]
    for i, vm in enumerate(vms):
        vm["name"] = f"win-{i}"
    out = await FindingsEngine(pricing=pricing).detect_windows_ahb(vms)
    assert len(out) == 1                       # ONE classified finding, not one per VM
    f = out[0]
    assert f["category"] == "windows_ahb"
    assert f["resource_id"] is None            # resource-less → escapes per-resource dedupe
    assert f["estimated_savings_monthly"] == round(67.16 + 537.28, 2)
    assert f["details"]["eligible_count"] == 2


async def test_windows_ahb_median_corrects_a_bad_price_fetch():
    # The Windows licence is a flat per-vCore charge, so it must be identical per vCore on every VM.
    # Two VMs price correctly (33.58/vCore); a third (like some B-series) returns a Windows price
    # barely above Linux — a bad fetch. The fleet MEDIAN must correct it, not understate its AHB.
    pricing = FakePricing(
        vm_prices={"Standard_D2s_v3": 70.08, "Standard_D4s_v3": 140.16, "Standard_B2ms": 60.0},
        win={"Standard_D2s_v3": 137.24,     # (137.24-70.08)/2  = 33.58/vCore  (good)
             "Standard_D4s_v3": 274.48,     # (274.48-140.16)/4 = 33.58/vCore  (good)
             "Standard_B2ms": 65.0},        # (65-60)/2 = 2.5/vCore            (bad fetch)
    )
    vms = []
    for i, sku in enumerate(("Standard_D2s_v3", "Standard_D4s_v3", "Standard_B2ms")):
        vm = _vm(max_cpu=40.0, sku=sku, rid=f"/s/vm-{i}")
        vm["name"] = f"win-{i}"
        vms.append(vm)
    out = await FindingsEngine(pricing=pricing).detect_windows_ahb(vms)
    items = {v["name"]: v for v in out[0]["details"]["eligible_vms"]}
    # B2ms's bad fetch alone would give ~₹5; the median corrects it to the fleet per-vCore rate.
    assert items["win-2"]["licence_charge"] > 50
    # Every VM ends up at the SAME per-vCore licence — that's the whole point of the fix.
    assert len({round(v["licence_charge"] / v["vcpu"], 1) for v in items.values()}) == 1


async def test_windows_ahb_empty_when_no_eligible_vms():
    engine = FindingsEngine(pricing=FakePricing())
    assert await engine.detect_windows_ahb([]) == []


async def test_grounded_aggregate_reads_as_validated_not_estimate():
    # An AHB/commitment finding derived from ACTUAL cost must show as cost-validated — not the
    # "estimate / upper bound" badge (which is exactly backwards for a grounded number).
    vm = _vm(max_cpu=40.0, sku="Standard_D2s_v3", rid=VM_RID); vm["name"] = "win"
    grounded_ahb = (await FindingsEngine(pricing=FakePricing(), cost_map={VM_RID.lower(): 100.0})
                    .detect_windows_ahb([vm]))[0]
    assert grounded_ahb["validation_status"] == "validated"
    ri = [f for f in await FindingsEngine(pricing=FakePricing(), cost_map={VM_RID.lower(): 80.0})
          .detect_vm_commitments([_steady_vm()]) if f["category"] == "ri_vm"][0]
    assert ri["validation_status"] == "validated"
    # Without billing data it's a genuine list-price estimate → NOT validated.
    ungrounded = (await FindingsEngine(pricing=FakePricing()).detect_windows_ahb([vm]))[0]
    assert ungrounded["validation_status"] in (None, "unvalidated")


async def test_ahb_matches_azure_calculator_to_the_dollar():
    # GOLDEN: Azure pricing calculator, D16s v3 (US) — Windows (licence incl.) $1,097.92/mo,
    # compute-only (Linux) $560.64/mo. AHB removes the licence = $537.28/mo = $6,447.36/yr.
    pricing = FakePricing(vm_prices={"Standard_D16s_v3": 560.64}, win={"Standard_D16s_v3": 1097.92})
    vm = _vm(max_cpu=40.0, sku="Standard_D16s_v3", rid="/s/win"); vm["name"] = "win"

    # No billing → list-price (full-time) basis == what the calculator shows.
    f = (await FindingsEngine(pricing=pricing).detect_windows_ahb([vm]))[0]
    assert f["estimated_savings_monthly"] == 537.28
    assert f["estimated_savings_annual"] == round(537.28 * 12, 2)   # 6,447.36
    item = f["details"]["eligible_vms"][0]
    assert item["windows_price"] == 1097.92 and item["compute_only_price"] == 560.64
    assert item["licence_charge"] == 537.28

    # Grounded: if the VM actually bills $300/mo, AHB = $300 x (537.28/1097.92).
    frac = (1097.92 - 560.64) / 1097.92
    grounded = FindingsEngine(pricing=pricing, cost_map={"/s/win": 300.0})
    fg = (await grounded.detect_windows_ahb([vm]))[0]
    assert fg["estimated_savings_monthly"] == round(300.0 * frac, 2)


async def test_windows_ahb_excludes_deleted_vms():
    # A Windows VM already recommended for deletion (idle) must NOT also earn an AHB licence saving —
    # otherwise its cost is double-counted (delete = full cost, AHB = licence, on the same VM).
    keep = _vm(max_cpu=40.0, sku="Standard_D2s_v3", rid="/s/keep"); keep["name"] = "keep"
    dele = _vm(max_cpu=2.0, sku="Standard_D16s_v3", rid="/s/idle"); dele["name"] = "idle"
    engine = FindingsEngine(pricing=FakePricing())  # no cost_map → retail path, both otherwise eligible
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


async def test_commitment_skips_non_billing_vm():
    # A steadily-"running" VM with no measured cost yields no real reservation saving → excluded.
    engine = FindingsEngine(pricing=FakePricing(), cost_map={"/s/other": 100.0})
    vm = _steady_vm(rid="/s/vm-demo")  # not in cost_map
    assert await engine.detect_vm_commitments([vm]) == []


async def test_windows_ahb_falls_back_to_retail_delta_without_cost():
    vm = _vm(max_cpu=40.0, sku="Standard_D2s_v3")
    engine = FindingsEngine(pricing=FakePricing())  # no cost_map
    f = (await engine.detect_windows_ahb([vm]))[0]
    assert f["estimated_savings_monthly"] == round(137.24 - 70.08, 2)  # raw retail fallback
    assert f["details"]["eligible_vms"][0]["actual_cost_based"] is False


async def test_commitment_grounds_as_fraction_of_actual_cost():
    # A commitment saves the DISCOUNT %, not the whole cost. D16 actually costs $80/mo (Linux VM →
    # full cost is compute), 3yr RI discount = (560.64−350)/560.64 = 0.3757 → best case 80×0.3757.
    engine = FindingsEngine(pricing=FakePricing(), cost_map={VM_RID.lower(): 80.0})
    ri = [f for f in await engine.detect_vm_commitments([_steady_vm()]) if f["category"] == "ri_vm"][0]
    ratio_3yr = (560.64 - 350) / 560.64
    ratio_1yr = (560.64 - 400) / 560.64
    assert ri["estimated_savings_monthly"] == round(80.0 * ratio_3yr, 2)   # 30.06, not 80
    assert ri["details"]["total_1yr_monthly"] == round(80.0 * ratio_1yr, 2)  # 22.92 — 1yr ≠ 3yr
    assert ri["details"]["total_1yr_monthly"] != ri["details"]["total_3yr_monthly"]


async def test_commitment_strips_windows_licence_from_base():
    # For a Windows VM not on AHB, the RI/SP discount applies to the COMPUTE portion only (Linux/
    # Windows fraction), so it doesn't overlap the separate AHB licence saving on the same VM.
    vm = _steady_vm(rid="/s/winvm")
    vm["vmSize"] = "Standard_D2s_v3"
    engine = FindingsEngine(pricing=FakePricing(), cost_map={"/s/winvm": 200.0})
    as_linux = (await engine.detect_vm_commitments([vm], set()))[0]["estimated_savings_monthly"]
    as_windows = (await engine.detect_vm_commitments([vm], {"/s/winvm"}))[0]["estimated_savings_monthly"]
    frac = 70.08 / 137.24  # Linux / Windows retail
    assert as_windows < as_linux
    assert abs(as_windows - as_linux * frac) < 0.1  # licence stripped from the base


async def test_backup_redundancy_rule():
    engine = FindingsEngine(pricing=FakePricing())
    f = (await engine.detect_orphans("geo_redundant_vaults", [
        {"id": "/s/vault-1", "name": "vault-1", "subscriptionId": "sub-1", "resourceGroup": "rg-a"},
    ]))[0]
    assert f["category"] == "backup_redundancy"
    assert f["estimated_savings_monthly"] == 25.0


# ── Broad-coverage orphan/waste (rule-driven) ────────────────────────────────────

def _row(rid, **extra):
    return {"id": rid, "name": "res-1", "subscriptionId": "sub-1", "resourceGroup": "rg-a",
            "location": "eastus", **extra}


async def test_orphan_snapshot_priced_by_size():
    engine = FindingsEngine(pricing=FakePricing())
    f = (await engine.detect_orphans("orphaned_snapshots", [_row("/s/snap-1", diskSizeGB=200)]))[0]
    assert f["category"] == "orphaned_snapshots"
    assert f["estimated_savings_monthly"] == round(200 * 0.05, 2)  # 10.0


async def test_empty_load_balancer_fixed_estimate():
    engine = FindingsEngine(pricing=FakePricing())
    f = (await engine.detect_orphans("empty_load_balancers", [_row("/s/lb-1", skuName="Standard")]))[0]
    assert f["category"] == "empty_load_balancers"
    assert f["estimated_savings_monthly"] == 18.0


async def test_idle_nat_gateway_fixed_estimate():
    engine = FindingsEngine(pricing=FakePricing())
    f = (await engine.detect_orphans("idle_nat_gateways", [_row("/s/nat-1")]))[0]
    assert f["category"] == "idle_nat_gateways"
    assert f["estimated_savings_monthly"] == 32.0


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


async def test_sql_ahb_grounds_on_vcores_capped_at_actual_cost():
    from app.services.findings import SQL_AHB_LICENCE_PER_VCORE_MONTHLY_USD as PVC
    db_rid = "/subscriptions/s/rg/providers/microsoft.sql/servers/srv/databases/db1"
    mi_rid = "/subscriptions/s/rg/providers/microsoft.sql/managedinstances/mi1"
    tiny_rid = "/subscriptions/s/rg/providers/microsoft.sql/servers/srv/databases/tiny"
    # db: 8 vCores × 112 = 896 < 2000 actual → 896. mi: 16 × 112 = 1792 < 5000 → 1792.
    # tiny: 4 × 112 = 448 but actual only 100 → capped to 100 (can't save more than you pay).
    cost_map = {db_rid.lower(): 2000.0, mi_rid.lower(): 5000.0, tiny_rid.lower(): 100.0}
    engine = FindingsEngine(pricing=FakePricing(), cost_map=cost_map)
    resources = [
        {"id": db_rid, "name": "db1", "subscriptionId": "s", "location": "eastus",
         "skuName": "GP_Gen5", "tier": "GeneralPurpose", "vcores": 8},
        {"id": mi_rid, "name": "mi1", "subscriptionId": "s", "location": "eastus",
         "skuName": "GP_Gen5", "tier": "GeneralPurpose", "vcores": 16},
        {"id": tiny_rid, "name": "tiny", "subscriptionId": "s", "location": "eastus",
         "skuName": "GP_Gen5", "tier": "GeneralPurpose", "vcores": 4},
    ]
    out = await engine.detect_sql_ahb(resources)
    assert len(out) == 1
    f = out[0]
    assert f["category"] == "sql_ahb"
    assert f["details"]["eligible_count"] == 3
    assert f["details"]["licence_kind"] == "SQL Server"
    assert f["estimated_savings_monthly"] == round(8 * PVC + 16 * PVC + 100.0, 2)
    assert f["validation_status"] == "validated"  # every one grounded in actual cost


async def test_sql_ahb_skips_non_billing_and_excluded():
    db_rid = "/subscriptions/s/rg/providers/microsoft.sql/servers/srv/databases/db1"
    dead_rid = "/subscriptions/s/rg/providers/microsoft.sql/servers/srv/databases/dead"
    engine = FindingsEngine(pricing=FakePricing(), cost_map={db_rid.lower(): 2000.0})
    resources = [
        {"id": db_rid, "name": "db1", "vcores": 8, "skuName": "GP_Gen5", "location": "eastus"},
        {"id": dead_rid, "name": "dead", "vcores": 8, "skuName": "GP_Gen5", "location": "eastus"},
    ]
    # dead has no measured cost → skipped; db1 is excluded (e.g. paused) → nothing left.
    out = await engine.detect_sql_ahb(resources, exclude_ids={db_rid.lower()})
    assert out == []


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
