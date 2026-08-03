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


# App Service Plans expose CPU + memory as direct percentages at the plan (serverfarm) level.
ASP_CPU_METRIC = "CpuPercentage"
ASP_MEMORY_METRIC = "MemoryPercentage"


async def get_asp_utilisation(
    client: AzureClient, resource_id: str, days: int = DEFAULT_WINDOW_DAYS
):
    """Return `(peak_cpu_pct, peak_memory_pct, datapoint_count)` for an App Service Plan.

    Peak (Maximum) over the window — a downsize must be safe at the busiest moment. Either metric may
    be empty (`None`); the caller treats missing data as "unverified", never as 0%.
    """
    cpu, mem = await asyncio.gather(
        client.get_metric(resource_id, ASP_CPU_METRIC, days=days, aggregation="Maximum"),
        client.get_metric(resource_id, ASP_MEMORY_METRIC, days=days, aggregation="Maximum"),
    )
    peak_cpu = round(max(cpu), 2) if cpu else None
    peak_mem = round(max(mem), 2) if mem else None
    return peak_cpu, peak_mem, max(len(cpu), len(mem))


async def enrich_asps_with_metrics(
    client: AzureClient, plans: List[Dict], days: int = DEFAULT_WINDOW_DAYS
) -> List[Dict]:
    """Attach peak CPU + memory % to each App Service Plan row, fetched in parallel (bounded)."""
    if not plans:
        return []
    sem = asyncio.Semaphore(_METRIC_CONCURRENCY)

    async def _bounded(plan_id: str):
        async with sem:
            return await get_asp_utilisation(client, plan_id, days)

    results = await asyncio.gather(*[_bounded(p.get("id", "")) for p in plans], return_exceptions=True)
    enriched: List[Dict] = []
    for plan, res in zip(plans, results):
        peak_cpu, peak_mem, dp = res if isinstance(res, tuple) else (None, None, 0)
        enriched.append({
            **plan, "max_cpu_pct": peak_cpu, "max_memory_pct": peak_mem,
            "metric_datapoints": dp, "metric_window_days": days,
        })
    return enriched


# SQL Database vCore load shows up across several dimensions — a DB can be CPU-light but IO-bound, so a
# safe downsize needs ALL of them low, not CPU alone.
SQL_CPU_METRIC = "cpu_percent"
SQL_DATA_IO_METRIC = "physical_data_read_percent"
SQL_LOG_IO_METRIC = "log_write_percent"


async def get_sql_db_utilisation(
    client: AzureClient, resource_id: str, days: int = DEFAULT_WINDOW_DAYS
):
    """Return `(peak_cpu, peak_data_io, peak_log_io, datapoint_count)` %s for a SQL Database.

    Peaks (Maximum) over the window — a downsize must be safe at the busiest moment. Any metric may be
    None (no data); the caller only downsizes when every AVAILABLE dimension clears the ceiling.
    """
    cpu, data_io, log_io = await asyncio.gather(
        client.get_metric(resource_id, SQL_CPU_METRIC, days=days, aggregation="Maximum"),
        client.get_metric(resource_id, SQL_DATA_IO_METRIC, days=days, aggregation="Maximum"),
        client.get_metric(resource_id, SQL_LOG_IO_METRIC, days=days, aggregation="Maximum"),
    )
    return (
        round(max(cpu), 2) if cpu else None,
        round(max(data_io), 2) if data_io else None,
        round(max(log_io), 2) if log_io else None,
        max(len(cpu), len(data_io), len(log_io)),
    )


async def enrich_sql_dbs_with_metrics(
    client: AzureClient, dbs: List[Dict], days: int = DEFAULT_WINDOW_DAYS
) -> List[Dict]:
    """Attach peak CPU / data-IO / log-IO % to each SQL Database row, fetched in parallel (bounded)."""
    if not dbs:
        return []
    sem = asyncio.Semaphore(_METRIC_CONCURRENCY)

    async def _bounded(db_id: str):
        async with sem:
            return await get_sql_db_utilisation(client, db_id, days)

    results = await asyncio.gather(*[_bounded(d.get("id", "")) for d in dbs], return_exceptions=True)
    enriched: List[Dict] = []
    for db, res in zip(dbs, results):
        cpu, data_io, log_io, dp = res if isinstance(res, tuple) else (None, None, None, 0)
        enriched.append({
            **db, "max_cpu_pct": cpu, "max_data_io_pct": data_io, "max_log_io_pct": log_io,
            "metric_datapoints": dp, "metric_window_days": days,
        })
    return enriched


# Managed-disk resource-level I/O metrics (microsoft.compute/disks). Peak IOPS = read+write ops/sec;
# peak throughput = read+write bytes/sec. Names verified against Azure Monitor supported metrics.
DISK_READ_OPS_METRIC = "Composite Disk Read Operations/sec"
DISK_WRITE_OPS_METRIC = "Composite Disk Write Operations/sec"
DISK_READ_BYTES_METRIC = "Composite Disk Read Bytes/sec"
DISK_WRITE_BYTES_METRIC = "Composite Disk Write Bytes/sec"


async def get_disk_io(client: AzureClient, resource_id: str, days: int = DEFAULT_WINDOW_DAYS):
    """Return `(peak_iops, peak_mbps, datapoint_count)` for a managed disk, or `(None, None, 0)`.

    Peak IOPS/throughput are the sum of the read+write peaks — an upper bound on real usage, so it
    makes a downgrade recommendation *less* likely (the safe direction).
    """
    ro, wo, rb, wb = await asyncio.gather(
        client.get_metric(resource_id, DISK_READ_OPS_METRIC, days=days, aggregation="Maximum"),
        client.get_metric(resource_id, DISK_WRITE_OPS_METRIC, days=days, aggregation="Maximum"),
        client.get_metric(resource_id, DISK_READ_BYTES_METRIC, days=days, aggregation="Maximum"),
        client.get_metric(resource_id, DISK_WRITE_BYTES_METRIC, days=days, aggregation="Maximum"),
    )
    if not (ro or wo or rb or wb):
        return None, None, 0
    peak_iops = (max(ro) if ro else 0) + (max(wo) if wo else 0)
    peak_mbps = ((max(rb) if rb else 0) + (max(wb) if wb else 0)) / 1_000_000
    return round(peak_iops, 1), round(peak_mbps, 1), max(len(ro), len(wo), len(rb), len(wb))


async def enrich_disks_with_iops(
    client: AzureClient, disks: List[Dict], days: int = DEFAULT_WINDOW_DAYS
) -> List[Dict]:
    """Attach peak IOPS + throughput (MB/s) to each managed-disk row, fetched in parallel (bounded)."""
    if not disks:
        return []
    sem = asyncio.Semaphore(_METRIC_CONCURRENCY)

    async def _bounded(disk_id: str):
        async with sem:
            return await get_disk_io(client, disk_id, days)

    results = await asyncio.gather(*[_bounded(d.get("id", "")) for d in disks], return_exceptions=True)
    enriched: List[Dict] = []
    for disk, res in zip(disks, results):
        peak_iops, peak_mbps, dp = res if isinstance(res, tuple) else (None, None, 0)
        enriched.append({
            **disk, "peak_iops": peak_iops, "peak_mbps": peak_mbps,
            "metric_datapoints": dp, "metric_window_days": days,
        })
    return enriched


# SQL Managed Instance exposes CPU as its primary throttleable signal (no per-DB IO-percent metrics).
SQL_MI_CPU_METRIC = "avg_cpu_percent"


async def enrich_sql_mis_with_metrics(
    client: AzureClient, instances: List[Dict], days: int = DEFAULT_WINDOW_DAYS
) -> List[Dict]:
    """Attach peak CPU % to each SQL Managed Instance row (Maximum of avg_cpu_percent over the window)."""
    if not instances:
        return []
    sem = asyncio.Semaphore(_METRIC_CONCURRENCY)

    async def _bounded(mi_id: str):
        async with sem:
            cpu = await client.get_metric(mi_id, SQL_MI_CPU_METRIC, days=days, aggregation="Maximum")
            return (round(max(cpu), 2) if cpu else None, len(cpu))

    results = await asyncio.gather(*[_bounded(m.get("id", "")) for m in instances], return_exceptions=True)
    enriched: List[Dict] = []
    for mi, res in zip(instances, results):
        cpu, dp = res if isinstance(res, tuple) else (None, 0)
        enriched.append({**mi, "max_cpu_pct": cpu, "metric_datapoints": dp, "metric_window_days": days})
    return enriched


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
