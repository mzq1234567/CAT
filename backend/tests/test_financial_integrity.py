"""
Financial-integrity tests — the tool must NEVER invent, assume, or fabricate a financial number.

One focused test per rule in the financial-integrity brief. Fixtures are reused from test_findings.
"""
from __future__ import annotations

from app.services.financial_evidence import (
    QUANTIFIED,
    REVIEW,
    counts_toward_total,
)
from app.services.findings import FindingsEngine
from app.services.pricing import PricingEngine

from tests.test_findings import FakePricing, _AHB_PRICING, _D16_FRAC, _row, _vm


# ── 1. Reserved Instances: authoritative price only, never fabricated ────────────────

def test_no_reservation_price_no_ri_finding():
    # A reservation group Azure returned with NO positive net saving must NOT become a finding — we never
    # invent a discount to make one appear.
    engine = FindingsEngine(pricing=FakePricing())
    group = {
        "category": "ri_vm", "sku": "Standard_D2s_v3", "region": "eastus", "product": "Virtual Machines",
        "subscription_id": "sub-1",
        "terms": {"P1Y": {"monthly_savings": 0.0, "monthly_ondemand": 70.0, "monthly_reserved": 70.0,
                          "quantity": 1}},
    }
    assert engine.commitments_from_recommendations([group]) == []


def test_ri_finding_uses_azure_savings_verbatim():
    # When Azure's engine DOES return a positive saving, we surface exactly that number — no tool-side
    # discount maths — and mark it grounded/authoritative.
    engine = FindingsEngine(pricing=FakePricing())
    group = {
        "category": "ri_vm", "sku": "Standard_D2s_v3", "region": "eastus", "product": "Virtual Machines",
        "subscription_id": "sub-1",
        "terms": {"P1Y": {"monthly_savings": 42.0, "monthly_ondemand": 70.0, "monthly_reserved": 28.0,
                          "quantity": 1}},
    }
    f = engine.commitments_from_recommendations([group])[0]
    assert f["category"] == "ri_vm"
    assert f["estimated_savings_monthly"] == 42.0                # exactly Azure's netSavings
    assert f["evidence_state"] == QUANTIFIED
    assert f["details"]["source"] == "azure_reservation_recommendations"


def test_no_fabricated_retail_reservation_price_path_exists():
    # The retail *reservation* price helper was removed so a generic list rate can never be mistaken for a
    # reservation saving. RI recommendations come solely from Azure's usage-based engine.
    assert not hasattr(PricingEngine, "get_vm_reserved_monthly_price")


# ── 2. Bastion / always-on with no billed cost → REVIEW (reference price only) ────────

async def test_bastion_without_billed_cost_is_review_not_a_saving():
    engine = FindingsEngine(pricing=FakePricing())               # empty cost_map (no billed cost)
    f = (await engine.detect_orphans("bastion_hosts", [_row("/s/bastion", skuName="Basic")]))[0]
    assert f["category"] == "bastion_hosts"
    assert f["evidence_state"] == REVIEW
    assert f["estimated_savings_monthly"] == 0.0                 # never a fabricated saving
    assert f["details"]["financial_impact"] == "Not quantified"
    assert f["details"]["reference_monthly_price"] == 138.0      # retail rate, reference only
    assert f["details"]["reference_price_source"] == "AUTHORITATIVE_RETAIL_PRICE"
    assert not counts_toward_total(f["evidence_state"], conditional=False)


async def test_bastion_with_billed_cost_is_quantified():
    engine = FindingsEngine(pricing=FakePricing(), cost_map={"/s/bastion": 90.0})
    f = (await engine.detect_orphans("bastion_hosts", [_row("/s/bastion", skuName="Basic")]))[0]
    assert f["evidence_state"] == QUANTIFIED
    assert f["estimated_savings_monthly"] == 90.0               # grounded in the ACTUAL billed cost


# ── 3. AHB — conditional/potential, ceilinged, excluded from totals ──────────────────

async def test_ahb_without_eligibility_is_potential_and_excluded_from_total():
    vm = _vm(max_cpu=40.0, sku="Standard_D16s_v3", rid="/s/win"); vm["name"] = "win"
    engine = FindingsEngine(pricing=_AHB_PRICING, cost_map={"/s/win": 300.0})
    f = (await engine.detect_windows_ahb([vm]))[0]
    assert f["details"]["conditional"] is True                  # realised only with eligible licences
    assert f["details"]["savings_source"] == "POTENTIAL_SAVINGS"
    # A conditional saving NEVER counts toward Total Identified Savings / Projected Spend.
    assert not counts_toward_total(f["evidence_state"], conditional=True)


async def test_ahb_saving_never_exceeds_the_eligible_spend_being_displaced():
    # Hard ceiling: AHB saving = actual billed × licence fraction, so it can never exceed the VM's own
    # actual cost (the Windows compute/licensing spend being displaced).
    vm = _vm(max_cpu=40.0, sku="Standard_D16s_v3", rid="/s/win"); vm["name"] = "win"
    actual = 300.0
    engine = FindingsEngine(pricing=_AHB_PRICING, cost_map={"/s/win": actual})
    f = (await engine.detect_windows_ahb([vm]))[0]
    assert f["estimated_savings_monthly"] == round(actual * _D16_FRAC, 2)
    assert f["estimated_savings_monthly"] <= actual             # never exceeds the eligible spend
    assert f["details"]["eligible_vms"][0]["monthly_savings"] <= actual


# ── 3b. Sponsored / credited subscriptions ───────────────────────────────────────────

async def test_sponsored_near_zero_billed_cost_is_review_not_a_tiny_saving():
    # A Bastion lists at 138/mo but is billed at 0.5/mo under an Azure Sponsorship credit. We must NOT
    # claim a $0.50 saving (implying the resource is worthless) NOR invent the list price as a saving —
    # the customer's realisable saving can't be quantified from a near-zero bill → REVIEW (reference only).
    engine = FindingsEngine(pricing=FakePricing(), cost_map={"/s/bastion": 0.5})
    f = (await engine.detect_orphans("bastion_hosts", [_row("/s/bastion", skuName="Basic")]))[0]
    assert f["evidence_state"] == REVIEW
    assert f["estimated_savings_monthly"] == 0.0
    assert f["details"]["sponsored_or_credited"] is True
    assert f["details"]["reference_monthly_price"] == 138.0     # economic value ≈ list price, reference only


async def test_normal_billed_cost_is_not_flagged_sponsored():
    # A resource billed at a normal fraction of list (even a discounted 65%) is genuine spend → QUANTIFIED.
    engine = FindingsEngine(pricing=FakePricing(), cost_map={"/s/bastion": 90.0})  # 90 of 138 list
    f = (await engine.detect_orphans("bastion_hosts", [_row("/s/bastion", skuName="Basic")]))[0]
    assert f["evidence_state"] == QUANTIFIED
    assert f["estimated_savings_monthly"] == 90.0
    assert "sponsored_or_credited" not in f["details"]


# ── 3c. Metrics failure → REVIEW, never idle / right-sized ───────────────────────────

async def test_metrics_failure_produces_review_not_idle():
    # A VM whose utilisation metrics FAILED to retrieve (metrics_failed=True, no CPU data) must never be
    # classified idle/oversized; it surfaces as a REVIEW "metrics unavailable" finding (evidence knows).
    vm = _vm(max_cpu=None, datapoints=0, sku="Standard_D2s_v3", rid="/s/vm1")
    vm["name"] = "vm1"
    vm["metrics_failed"] = True
    engine = FindingsEngine(pricing=FakePricing(), cost_map={"/s/vm1": 100.0})
    out = await engine.detect_vm_utilisation_findings([vm])
    cats = {f["category"] for f in out}
    assert "idle_vms" not in cats and "oversized_vms" not in cats     # never fabricated from missing data
    review = [f for f in out if f["category"] == "vm_metrics_unavailable"]
    assert len(review) == 1
    f = review[0]
    assert f["evidence_state"] == REVIEW
    assert f["estimated_savings_monthly"] == 0.0
    assert f["details"]["reason"] == "metrics_unavailable" and f["details"]["affected_count"] == 1
    assert not counts_toward_total(f["evidence_state"], conditional=False)


async def test_pricing_failure_never_fabricates_a_quantified_saving():
    # A pricing failure (live price unavailable) for a utilisation-dependent recommendation must never
    # become a fabricated quantified saving — the finding carries no positive quantified figure.
    from app.services.pricing import PricingUnavailableError

    class _NoVmPrice(FakePricing):
        async def get_vm_monthly_price(self, region, sku):
            raise PricingUnavailableError("retail prices unavailable")

    vm = _vm(max_cpu=2.0, peak_memory=2.0, sku="Standard_D2s_v3", rid="/s/vmp")
    vm["name"] = "vmp"
    engine = FindingsEngine(pricing=_NoVmPrice(), cost_map={"/s/vmp": 100.0})
    out = await engine.detect_vm_utilisation_findings([vm])
    fabricated = [f for f in out
                  if f["evidence_state"] == "quantified" and f["estimated_savings_monthly"] > 0]
    assert fabricated == []                                  # no price → no fabricated quantified saving


async def test_genuinely_empty_metrics_are_not_flagged_review():
    # Azure genuinely returned no datapoints (NOT a failure) → no finding at all (legitimate "no data"),
    # not a REVIEW — we only REVIEW when WE failed to retrieve.
    vm = _vm(max_cpu=None, datapoints=0, sku="Standard_D2s_v3", rid="/s/vm2")
    vm["name"] = "vm2"                                       # metrics_failed not set → genuine empty
    engine = FindingsEngine(pricing=FakePricing(), cost_map={"/s/vm2": 100.0})
    assert await engine.detect_vm_utilisation_findings([vm]) == []


# ── 4. Overlapping recommendations → non-overlapping total ───────────────────────────

def test_overlapping_findings_on_one_resource_are_deduped():
    from app.services import assessment as pipeline
    rid = "/subscriptions/s/resourceGroups/rg/providers/microsoft.compute/virtualmachines/vm-1"
    findings = [
        {"resource_id": rid, "category": "idle_vms", "estimated_savings_annual": 1200.0},
        {"resource_id": rid, "category": "advisor_cost", "estimated_savings_annual": 400.0},
    ]
    out = pipeline._dedupe(findings)
    # One optimisation per resource — the two overlapping findings collapse to the larger one only.
    assert len(out) == 1 and out[0]["estimated_savings_annual"] == 1200.0


# ── RI vs right-sizing overlap (counted_savings) ─────────────────────────────────────

def _ri(sku, region, qty, total_saving):
    """An aggregated ri_vm finding for `qty` instances of `sku` in `region` (Azure netSavings is the
    TOTAL for the recommended quantity, so per-instance = total_saving / qty)."""
    return {
        "category": "ri_vm",
        "estimated_savings_monthly": total_saving, "estimated_savings_annual": total_saving * 12,
        "counted_savings_monthly": total_saving, "counted_savings_annual": total_saving * 12,
        "details": {"reservation_items": [{"sku": sku, "region": region, "quantity": qty,
                                           "monthly_savings": total_saving, "monthly_savings_3yr": None}],
                    "total_3yr_monthly": None},
    }


def _rs(category, sku, region, saving):
    """A per-VM right-sizing/idle finding of `saving`/mo for a VM of `sku` in `region`."""
    return {
        "category": category,
        "estimated_savings_monthly": saving, "estimated_savings_annual": saving * 12,
        "counted_savings_monthly": saving, "counted_savings_annual": saving * 12,
        "details": {"vm_sku": sku, "vm_region": region},
    }


def _non_overlapping_total(findings):
    from app.services.financial_evidence import counts_toward_total
    from app.services.findings import CONDITIONAL_CATEGORIES
    return round(sum(
        f.get("counted_savings_monthly", 0.0) for f in findings
        if counts_toward_total(f.get("evidence_state", "quantified"), f["category"] in CONDITIONAL_CATEGORIES)
    ), 2)


def test_overlap_ri_only():
    from app.services.assessment import resolve_overlaps
    findings = resolve_overlaps([_ri("Standard_D2s_v3", "eastus", 1, 40.0)])
    assert findings[0]["counted_savings_monthly"] == 40.0          # unchanged, nothing overlaps
    assert _non_overlapping_total(findings) == 40.0


def test_overlap_rightsizing_only():
    from app.services.assessment import resolve_overlaps
    findings = resolve_overlaps([_rs("oversized_vms", "Standard_D2s_v3", "eastus", 30.0)])
    assert findings[0]["counted_savings_monthly"] == 30.0          # unchanged, no RI to overlap
    assert _non_overlapping_total(findings) == 30.0


def test_overlap_both_on_same_vm_counts_only_the_larger():
    from app.services.assessment import resolve_overlaps
    # RI would save 40/mo on this VM; right-sizing 30/mo. They're mutually exclusive → count only 40, not 70.
    ri = _ri("Standard_D2s_v3", "eastus", 1, 40.0)
    rs = _rs("oversized_vms", "Standard_D2s_v3", "eastus", 30.0)
    findings = resolve_overlaps([ri, rs])
    assert ri["counted_savings_monthly"] == 40.0                   # RI wins (larger)
    assert rs["counted_savings_monthly"] == 0.0                    # superseded — excluded from total
    assert rs["estimated_savings_monthly"] == 30.0                 # still DISPLAYED individually
    assert rs["details"]["overlap_superseded_by_ri"] is True
    assert _non_overlapping_total(findings) == 40.0                # never 70


def test_overlap_rightsizing_larger_than_ri_wins():
    from app.services.assessment import resolve_overlaps
    ri = _ri("Standard_D2s_v3", "eastus", 1, 40.0)
    rs = _rs("oversized_vms", "Standard_D2s_v3", "eastus", 55.0)   # right-sizing saves more than RI
    findings = resolve_overlaps([ri, rs])
    assert rs["counted_savings_monthly"] == 55.0                   # right-sizing wins
    assert ri["counted_savings_monthly"] == 0.0                    # RI reduced by the overlapping instance
    assert _non_overlapping_total(findings) == 55.0               # never 95


def test_overlap_multiple_vms_mixed_states():
    from app.services.assessment import resolve_overlaps
    # RI covers 2× D2s_v3 in eastus (80/mo total → 40 per instance). One D2s_v3 VM right-sizes for 30
    # (RI wins), another for 55 (right-sizing wins). A D4s_v3 in westus right-sizes for 20 (no RI overlap).
    ri = _ri("Standard_D2s_v3", "eastus", 2, 80.0)
    rs_small = _rs("oversized_vms", "Standard_D2s_v3", "eastus", 30.0)
    rs_big = _rs("idle_vms", "Standard_D2s_v3", "eastus", 55.0)
    rs_other = _rs("oversized_vms", "Standard_D4s_v3", "westus", 20.0)
    findings = resolve_overlaps([ri, rs_small, rs_big, rs_other])
    assert rs_small["counted_savings_monthly"] == 0.0             # RI wins this instance
    assert rs_big["counted_savings_monthly"] == 55.0             # right-sizing wins this instance
    assert rs_other["counted_savings_monthly"] == 20.0          # no overlap
    assert ri["counted_savings_monthly"] == 40.0                # 80 − one 40 instance ceded to right-sizing
    # Non-overlapping total = 40 (RI) + 55 (rs_big) + 20 (rs_other); the 30 finding is superseded. Never 185.
    assert _non_overlapping_total(findings) == 115.0


# ── Reserved-capacity vs right-sizing (disclosure, not silent double-count) ───────────

def _reserved(category, sub=""):
    return {"category": category, "subscription_id": sub,
            "estimated_savings_monthly": 100.0, "estimated_savings_annual": 1200.0,
            "counted_savings_monthly": 100.0, "counted_savings_annual": 1200.0, "details": {}}


def _rightsize(category, sub="", saving=30.0):
    return {"category": category, "subscription_id": sub,
            "estimated_savings_monthly": saving, "estimated_savings_annual": saving * 12,
            "counted_savings_monthly": saving, "counted_savings_annual": saving * 12, "details": {}}


def test_reserved_vs_rightsizing_same_family_is_disclosed():
    from app.services.assessment import flag_reservation_rightsizing_overlaps
    rs = _rightsize("sql_db_rightsizing", sub="sub-1")
    out = flag_reservation_rightsizing_overlaps([_reserved("sql_db_reserved_capacity", sub="sub-1"), rs])
    # Disclosure flag stamped on the per-resource finding; totals are NEVER altered by this pass.
    assert rs["details"]["mutually_exclusive_with_reservation"]
    assert rs["counted_savings_monthly"] == 30.0
    assert _non_overlapping_total(out) == 130.0   # both still count — we disclose, we don't fabricate


def test_reserved_vs_rightsizing_different_family_not_flagged():
    from app.services.assessment import flag_reservation_rightsizing_overlaps
    rs = _rightsize("disk_rightsizing", sub="sub-1")
    flag_reservation_rightsizing_overlaps([_reserved("sql_db_reserved_capacity", sub="sub-1"), rs])
    assert "mutually_exclusive_with_reservation" not in rs["details"]  # disk vs SQL — unrelated


def test_reserved_vs_rightsizing_other_subscription_not_flagged():
    from app.services.assessment import flag_reservation_rightsizing_overlaps
    rs = _rightsize("sql_db_rightsizing", sub="sub-2")
    flag_reservation_rightsizing_overlaps([_reserved("sql_db_reserved_capacity", sub="sub-1"), rs])
    assert "mutually_exclusive_with_reservation" not in rs["details"]  # different subs → no overlap


def test_reserved_vs_rightsizing_aggregate_reservation_is_wildcard():
    from app.services.assessment import flag_reservation_rightsizing_overlaps
    # An aggregate reservation with no subscription id (blank) is treated as a wildcard → still disclosed.
    rs = _rightsize("app_service_plan_rightsizing", sub="sub-9")
    flag_reservation_rightsizing_overlaps([_reserved("app_service_reserved_capacity", sub=""), rs])
    assert rs["details"]["mutually_exclusive_with_reservation"]


def test_no_reservation_means_no_flags():
    from app.services.assessment import flag_reservation_rightsizing_overlaps
    rs = _rightsize("sql_db_rightsizing", sub="sub-1")
    flag_reservation_rightsizing_overlaps([rs])
    assert "mutually_exclusive_with_reservation" not in rs["details"]


# ── 5. Insufficient billing history → REVIEW (no confident annualisation) ─────────────

async def test_insufficient_billing_history_is_review_not_annualised():
    # A run-rate saving over only 5 days of billing can't be defensibly annualised → REVIEW.
    disk = _row("/s/disk", skuName="StandardSSD_LRS", diskSizeGB=128)
    disk["name"] = "disk"
    engine = FindingsEngine(
        pricing=FakePricing(),
        cost_map={"/s/disk": 20.0},
        cost_basis={"/s/disk": {"cost_basis": "run_rate", "cost_is_estimate": True}},
        billing_span_days=5,                                    # < MIN_BILLING_DAYS_FOR_ANNUAL
    )
    f = (await engine.detect_unattached_disks([disk]))[0]
    assert f["evidence_state"] == REVIEW
    assert f["estimated_savings_monthly"] == 0.0
    assert f["details"]["insufficient_billing_history"] is True
    assert f["details"]["billing_span_days"] == 5


async def test_sufficient_billing_history_is_quantified():
    disk = _row("/s/disk", skuName="StandardSSD_LRS", diskSizeGB=128)
    disk["name"] = "disk"
    engine = FindingsEngine(
        pricing=FakePricing(),
        cost_map={"/s/disk": 20.0},
        cost_basis={"/s/disk": {"cost_basis": "run_rate", "cost_is_estimate": True}},
        billing_span_days=30,                                   # a full month of history → fine
    )
    f = (await engine.detect_unattached_disks([disk]))[0]
    assert f["evidence_state"] == QUANTIFIED
    assert f["estimated_savings_monthly"] == 20.0


# ── 6. Projected spend / totals rule — quantified, non-conditional only ──────────────

def test_counts_toward_total_rule():
    assert counts_toward_total(QUANTIFIED, conditional=False) is True      # a real saving counts
    assert counts_toward_total(QUANTIFIED, conditional=True) is False      # AHB potential never counts
    assert counts_toward_total(REVIEW, conditional=False) is False         # "not quantified" never counts
