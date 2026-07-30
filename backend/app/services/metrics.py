"""
Utilisation metrics enrichment (methodology: peak CPU + peak memory over 30 days).

ARG tells us a VM is *running* but not whether it is *busy*. This module pulls CPU and memory
from Azure Monitor so the findings engine can classify a running VM as idle, a downsize candidate,
or well-utilised — and, critically, checked on BOTH signals, not CPU alone. A VM can be CPU-idle
while doing real work in memory (a cache, a large in-process buffer) — CPU alone would wrongly
call it idle.

Methodology:
  * CPU:    peak (Maximum "Percentage CPU") over the window — a shutdown/downsize decision must be
            safe at the busiest moment; averaging hides scheduled/batch spikes.
  * Memory: peak pressure over the window, i.e. the *minimum* "Available Memory Percentage" —
            same worst-case principle, expressed from the "available" side. This is a direct Azure
            Monitor metric (same simple ARM metrics call as CPU) — no byte-to-percent conversion
            needed. It may still be empty for some VMs/images; that is handled as "unavailable",
            not assumed.
"""
from __future__ import annotations

import asyncio
from typing import Dict, List, Optional

from .azure_client import AzureClient

CPU_METRIC = "Percentage CPU"
MEMORY_METRIC = "Available Memory Percentage"
DEFAULT_WINDOW_DAYS = 30
# Cap concurrent Azure Monitor calls so a large fleet (hundreds of VMs × 3 metric calls each) doesn't
# self-throttle by firing everything at once. Metrics degrade per-VM, so a bounded pool is plenty.
_METRIC_CONCURRENCY = 15


class VmUtilisation:
    """Peak CPU/memory usage for one VM over the observation window."""

    __slots__ = ("avg_cpu", "max_cpu", "cpu_datapoints", "peak_memory_used_pct", "memory_datapoints", "window_days")

    def __init__(
        self, avg_cpu: Optional[float], max_cpu: Optional[float], cpu_datapoints: int,
        peak_memory_used_pct: Optional[float], memory_datapoints: int, window_days: int,
    ):
        self.avg_cpu = avg_cpu
        self.max_cpu = max_cpu
        self.cpu_datapoints = cpu_datapoints
        self.peak_memory_used_pct = peak_memory_used_pct
        self.memory_datapoints = memory_datapoints
        self.window_days = window_days

    @property
    def memory_available(self) -> bool:
        return self.peak_memory_used_pct is not None and self.memory_datapoints > 0


async def get_vm_cpu_stats(
    client: AzureClient, resource_id: str, days: int = DEFAULT_WINDOW_DAYS
):
    """Return `(avg_cpu, max_cpu, datapoint_count)` over the window. `(None, None, 0)` if no data."""
    avg_values = await client.get_metric(resource_id, CPU_METRIC, days=days, aggregation="Average")
    max_values = await client.get_metric(resource_id, CPU_METRIC, days=days, aggregation="Maximum")
    if not avg_values and not max_values:
        return None, None, 0
    avg_cpu = round(sum(avg_values) / len(avg_values), 2) if avg_values else None
    max_cpu = round(max(max_values), 2) if max_values else None
    count = max(len(avg_values), len(max_values))
    return avg_cpu, max_cpu, count


async def get_vm_memory_stats(
    client: AzureClient, resource_id: str, days: int = DEFAULT_WINDOW_DAYS
):
    """Return `(peak_memory_used_pct, datapoint_count)` — the worst-case (least-available) moment.

    `(None, 0)` when the metric has no data for this VM (older image, agent not reporting, etc.) —
    callers must treat that as "memory could not be verified", never assume it means 0% used.
    """
    available_values = await client.get_metric(
        resource_id, MEMORY_METRIC, days=days, aggregation="Minimum"
    )
    if not available_values:
        return None, 0
    min_available_pct = min(available_values)
    peak_used_pct = round(max(0.0, 100.0 - min_available_pct), 2)
    return peak_used_pct, len(available_values)


async def get_vm_utilisation(
    client: AzureClient, resource_id: str, days: int = DEFAULT_WINDOW_DAYS
) -> VmUtilisation:
    """Fetch CPU + memory stats for one VM in parallel."""
    (avg_cpu, max_cpu, cpu_dp), (mem_pct, mem_dp) = await asyncio.gather(
        get_vm_cpu_stats(client, resource_id, days),
        get_vm_memory_stats(client, resource_id, days),
    )
    return VmUtilisation(avg_cpu, max_cpu, cpu_dp, mem_pct, mem_dp, days)


async def enrich_vms_with_metrics(
    client: AzureClient, vms: List[Dict], days: int = DEFAULT_WINDOW_DAYS
) -> List[Dict]:
    """Attach CPU + memory utilisation fields to each VM row, fetched in parallel across VMs.

    A metrics failure for one VM leaves it un-classifiable (max_cpu=None) rather than failing the
    whole run. Missing memory data is distinct from missing CPU data — a VM can have one without
    the other, and the findings engine treats "memory unknown" as its own case, not "memory low".
    """
    if not vms:
        return []
    sem = asyncio.Semaphore(_METRIC_CONCURRENCY)

    async def _bounded(vm_id: str) -> VmUtilisation:
        async with sem:
            return await get_vm_utilisation(client, vm_id, days)

    tasks = [_bounded(vm.get("id", "")) for vm in vms]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    enriched: List[Dict] = []
    for vm, result in zip(vms, results):
        if isinstance(result, VmUtilisation):
            u = result
        else:
            u = VmUtilisation(None, None, 0, None, 0, days)
        enriched.append({
            **vm,
            "avg_cpu": u.avg_cpu,
            "max_cpu": u.max_cpu,
            "cpu_datapoints": u.cpu_datapoints,
            "peak_memory_used_pct": u.peak_memory_used_pct,
            "memory_datapoints": u.memory_datapoints,
            "memory_available": u.memory_available,
            "metric_window_days": days,
        })
    return enriched
