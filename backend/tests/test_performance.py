"""
Performance measurement — the detection engine's CPU/logic scaling at 10/100/500/1000 resources.

This measures the pipeline's OWN work (finding detection over inventory + metrics), with Azure fully
stubbed, so it captures how the engine scales with environment size independent of network latency.
Azure API round-trips dominate wall-clock in production and are bounded separately by the bounded-
concurrency + retry layer (see AZURE_DATA_COLLECTION_AUDIT.md); this test isolates the compute.

It asserts two things that matter for a client demo:
  * every size completes well within a generous ceiling (no accidental O(n^2) blow-up), and
  * per-resource time stays roughly flat as N grows 10 → 1000 (near-linear total scaling).
The numbers are printed so regressions are visible in test output.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.services.assessment import _dedupe, flag_reservation_rightsizing_overlaps, resolve_overlaps
from app.services.findings import FindingsEngine

from tests.test_findings import FakePricing

SIZES = [10, 100, 500, 1000]
# Generous ceiling per size — real machines vary; this only catches pathological (super-linear) blow-ups.
CEILING_SECONDS = {10: 0.5, 100: 1.0, 500: 3.0, 1000: 6.0}


def _make_vms(n: int):
    """A realistic mix: ~⅓ idle, ~⅓ oversized, ~⅓ healthy — each with full metrics so every VM detector
    (idle/oversized/AHB) does real work."""
    vms = []
    for i in range(n):
        r = i % 3
        max_cpu = 3.0 if r == 0 else (15.0 if r == 1 else 65.0)
        peak_mem = 6.0 if r == 0 else (20.0 if r == 1 else 70.0)
        vms.append({
            "id": f"/subscriptions/sub-1/resourceGroups/rg/providers/microsoft.compute/virtualmachines/vm-{i}",
            "name": f"vm-{i}", "subscriptionId": "sub-1", "resourceGroup": "rg",
            "location": "eastus", "vmSize": "Standard_D16s_v3",
            "avg_cpu": 1.0, "max_cpu": max_cpu, "peak_memory_used_pct": peak_mem,
            "memory_available": True, "cpu_datapoints": 30, "metric_window_days": 30,
            "osType": "Windows", "licenseType": None,
        })
    return vms


def _make_disks(n: int):
    return [{
        "id": f"/subscriptions/sub-1/resourceGroups/rg/providers/microsoft.compute/disks/disk-{i}",
        "name": f"disk-{i}", "subscriptionId": "sub-1", "resourceGroup": "rg",
        "location": "eastus", "skuName": "Premium_LRS", "diskSizeGB": 128,
        "managedBy": None,  # unattached
    } for i in range(n)]


def _make_orphans(n: int, bucket_type: str):
    return [{"id": f"/subscriptions/sub-1/resourceGroups/rg/providers/{bucket_type}/r-{i}",
             "name": f"r-{i}", "subscriptionId": "sub-1", "resourceGroup": "rg", "location": "eastus"}
            for i in range(n)]


async def _run_detection(n: int):
    """One full detection pass over N of each major resource type, grounded with a per-resource cost map."""
    vms = _make_vms(n)
    disks = _make_disks(n)
    ips = _make_orphans(n, "microsoft.network/publicipaddresses")
    cost_map = {v["id"].lower(): 120.0 for v in vms}
    cost_map.update({d["id"].lower(): 20.0 for d in disks})
    engine = FindingsEngine(pricing=FakePricing(), cost_map=cost_map, currency="USD")

    findings = []
    findings += await engine.detect_vm_utilisation_findings(vms)
    findings += await engine.detect_windows_ahb(vms)
    findings += await engine.detect_unattached_disks(disks)
    findings += await engine.detect_orphans("orphaned_public_ips", ips)
    # Include the overlap/dedupe reconciliation — it runs on every real assessment.
    return flag_reservation_rightsizing_overlaps(resolve_overlaps(_dedupe(findings)))


@pytest.mark.parametrize("n", SIZES)
def test_detection_scales_within_ceiling(n):
    start = time.perf_counter()
    findings = asyncio.run(_run_detection(n))
    elapsed = time.perf_counter() - start
    per_resource_ms = (elapsed / (n * 3)) * 1000  # 3 resource types × n
    print(f"\n[perf] N={n:>4} ({n*3} resources): {elapsed*1000:7.1f} ms total, "
          f"{per_resource_ms:5.3f} ms/resource, {len(findings)} findings")
    assert findings, "detection should produce findings"
    assert elapsed < CEILING_SECONDS[n], (
        f"detection of {n*3} resources took {elapsed:.2f}s (ceiling {CEILING_SECONDS[n]}s)")


def test_detection_scaling_is_near_linear():
    """Per-resource time at N=1000 must not be dramatically worse than at N=10 (guards against a hidden
    O(n^2) path — e.g. an accidental nested scan over all resources per resource)."""
    def per_resource_seconds(n):
        start = time.perf_counter()
        asyncio.run(_run_detection(n))
        return (time.perf_counter() - start) / (n * 3)

    small = per_resource_seconds(10)
    large = per_resource_seconds(1000)
    # Allow generous slack for fixed overhead amortised differently at small N, but reject super-linear growth.
    assert large < small * 5 + 0.001, (
        f"per-resource time grew from {small*1e6:.1f}µs (N=10) to {large*1e6:.1f}µs (N=1000) — "
        "worse than near-linear scaling")
