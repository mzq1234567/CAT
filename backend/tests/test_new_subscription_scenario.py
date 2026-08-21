"""
New / recently-migrated subscription scenario: current resources exist, but Azure Cost Management has
no billing history yet (and Azure's usage-based reservation engine has nothing to recommend).

Verifies the real-world behaviour reported from the migrated subscription:
  A. Resource-based findings (unattached disk, orphan, etc.) STILL surface from current inventory, as
     REVIEW ("not quantified"), and are never dropped just because billing is unavailable.
  B. Billing-dependent findings (RI / AHB / right-sizing / deallocated residual cost) quantify NOTHING
     without billed cost — never a fabricated or retail-substituted saving.
  C. Migration isolation: a stale recommendation scoped to the OLD subscription never attaches to the
     resources now in the NEW subscription.
  D. When billing later becomes available, the SAME resource quantifies from its ACTUAL cost, with no
     hardcoded/retail fallback ever used as the saving.

TEST ONLY — reuses the existing detector helpers; no production code is changed.
"""
from __future__ import annotations

from app.services.findings import FindingsEngine
from app.services.reservations import reconcile_vm_recommendations
from tests.test_findings import FakePricing, _current_vm, _disk, _vm, _vm_ri_group


def _deallocated_vm_no_cost():
    return {"id": "/subscriptions/new-sub/rg/providers/microsoft.compute/virtualmachines/dv",
            "name": "dv", "subscriptionId": "new-sub", "vmSize": "Standard_D2s_v3",
            "powerState": "VM deallocated",
            "osDiskId": "/subscriptions/new-sub/rg/providers/microsoft.compute/disks/dv-os",
            "dataDisks": []}


# ── A. resource-based finding surfaces without billing (as REVIEW) ─────────────────────────────────
async def test_resource_based_finding_surfaces_without_billing_as_review():
    # Empty cost_map == a subscription with no Cost Management history yet.
    engine = FindingsEngine(pricing=FakePricing())
    out = await engine.detect_unattached_disks([_disk()])
    assert len(out) == 1, "the orphan must still be detected from current inventory"
    f = out[0]
    assert f["category"] == "unattached_managed_disks"
    assert f["evidence_state"] == "review"                 # surfaced as 'not quantified'
    assert f["estimated_savings_monthly"] == 0.0           # never a fabricated saving
    # A retail reference may be shown, but it is NEVER used as the saving.
    assert f["details"].get("reference_monthly_price") == 19.71
    assert f["estimated_savings_monthly"] != 19.71


# ── B. billing-dependent detectors quantify nothing without billed cost ────────────────────────────
async def test_billing_dependent_detectors_produce_nothing_without_billing():
    engine = FindingsEngine(pricing=FakePricing())          # no cost_map
    # VM right-sizing needs per-resource cost to cap the delta → nothing without billing.
    assert await engine.detect_vm_utilisation_findings([_vm(max_cpu=15.0, peak_memory=20.0)]) == []
    # AHB grounds in the VM's ACTUAL billed cost → excluded (never priced at list) without billing.
    ahb_vm = _vm(max_cpu=40.0, sku="Standard_D16s_v3", rid="/subscriptions/new-sub/.../win")
    ahb_vm["name"] = "win"
    assert await engine.detect_windows_ahb([ahb_vm]) == []
    # Deallocated VM residual saving needs the disks' billed cost → dropped without it.
    assert engine.detect_deallocated_vms([_deallocated_vm_no_cost()]) == []
    # SQL DB right-sizing requires cost data on a stateful DB → nothing.
    assert await engine.detect_sql_db_rightsizing([{
        "id": "/subscriptions/new-sub/.../db", "tier": "GeneralPurpose", "skuName": "GP_Gen5",
        "vcores": 8, "max_cpu_pct": 5.0, "max_data_io_pct": 5.0, "max_log_io_pct": 5.0,
        "metric_datapoints": 30}]) == []
    # Reserved Instances come only from Azure's usage-based engine; a new sub has no recommendations.
    assert engine.commitments_from_recommendations([]) == []


# ── C. migration isolation: old-subscription rec never attaches to new-subscription resources ──────
async def test_migrated_vm_stale_old_sub_rec_does_not_attach_in_new_subscription():
    # The VM moved old-sub -> new-sub. A stale RI rec still scoped to old-sub must NOT produce a finding
    # and must NOT re-attach to the same-SKU VM now living in the new subscription.
    engine = FindingsEngine(pricing=FakePricing())
    stale_old_rec = _vm_ri_group(sku="Standard_D2s_v3", subscription_id="old-sub")
    current_new_inventory = [_current_vm(sku="Standard_D2s_v3", subscription_id="new-sub", name="moved-vm")]
    reconciled = reconcile_vm_recommendations([stale_old_rec], current_new_inventory, assessment_id=1)
    assert engine.commitments_from_recommendations(reconciled) == []


async def test_new_subscription_with_zero_current_vms_yields_no_vm_ri():
    # New sub, no current VMs yet + a historical rec → excluded (no current resource to reconcile to).
    engine = FindingsEngine(pricing=FakePricing())
    reconciled = reconcile_vm_recommendations(
        [_vm_ri_group(sku="Standard_D2s_v3", subscription_id="new-sub")], current_vms=[], assessment_id=1)
    assert engine.commitments_from_recommendations(reconciled) == []


# ── D. billing becomes available later: same resource quantifies from actual cost, no fallback ─────
async def test_same_resource_quantifies_once_billing_available_no_hardcoded_fallback():
    disk = _disk()
    no_billing = (await FindingsEngine(pricing=FakePricing()).detect_unattached_disks([disk]))[0]
    assert no_billing["evidence_state"] == "review"
    assert no_billing["estimated_savings_monthly"] == 0.0

    # Later, real Cost Management data exists for the SAME disk → quantified from the ACTUAL bill (88.0),
    # NOT the 19.71 retail reference (proves there is no hardcoded/retail fallback for the saving).
    quantified = (await FindingsEngine(pricing=FakePricing(), cost_map={disk["id"].lower(): 88.0})
                  .detect_unattached_disks([disk]))[0]
    assert quantified["evidence_state"] == "quantified"
    assert quantified["estimated_savings_monthly"] == 88.0
    assert quantified["validation_status"] == "validated"
