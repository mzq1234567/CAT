"""Tests for metrics enrichment: peak CPU + peak memory over the window."""
from __future__ import annotations

import httpx

from app.services.azure_client import AzureClient
from app.services.metrics import (
    enrich_vms_with_metrics,
    get_vm_cpu_stats,
    get_vm_memory_stats,
    get_vm_utilisation,
)
from tests.azure_mocks import ARG_RUNNING_VM, metrics_handler


# ── CPU ──────────────────────────────────────────────────────────────────────────

async def test_get_vm_cpu_stats_returns_avg_and_peak():
    handler = metrics_handler([1.0, 3.0, 5.0], max_values=[10.0, 20.0, 44.0])
    client = AzureClient("t", transport=httpx.MockTransport(handler))
    avg, mx, count = await get_vm_cpu_stats(client, "/subscriptions/s/vm-1")
    assert avg == 3.0
    assert mx == 44.0
    assert count == 3


async def test_get_vm_cpu_stats_no_data():
    client = AzureClient("t", transport=httpx.MockTransport(metrics_handler([], status=403)))
    avg, mx, count = await get_vm_cpu_stats(client, "/subscriptions/s/vm-1")
    assert avg is None and mx is None and count == 0


# ── Memory (direct "Available Memory Percentage") ───────────────────────────────

async def test_get_vm_memory_stats_computes_peak_used_from_min_available():
    # Available% samples [95, 70, 56, 80] → min available 56% → peak used = 44%.
    handler = metrics_handler([1.0], memory_available_values=[95.0, 70.0, 56.0, 80.0])
    client = AzureClient("t", transport=httpx.MockTransport(handler))
    peak_used, count = await get_vm_memory_stats(client, "/subscriptions/s/vm-1")
    assert peak_used == 44.0
    assert count == 4


async def test_get_vm_memory_stats_unavailable_returns_none():
    handler = metrics_handler([1.0], memory_available_values=None)
    client = AzureClient("t", transport=httpx.MockTransport(handler))
    peak_used, count = await get_vm_memory_stats(client, "/subscriptions/s/vm-1")
    assert peak_used is None
    assert count == 0


async def test_get_vm_memory_stats_denied():
    handler = metrics_handler([1.0], memory_status=403)
    client = AzureClient("t", transport=httpx.MockTransport(handler))
    peak_used, count = await get_vm_memory_stats(client, "/subscriptions/s/vm-1")
    assert peak_used is None and count == 0


# ── Combined ─────────────────────────────────────────────────────────────────────

async def test_get_vm_utilisation_combines_cpu_and_memory():
    handler = metrics_handler([1.0, 2.0], max_values=[3.0, 44.0], memory_available_values=[60.0, 56.0])
    client = AzureClient("t", transport=httpx.MockTransport(handler))
    u = await get_vm_utilisation(client, "/subscriptions/s/vm-1")
    assert u.max_cpu == 44.0
    assert u.peak_memory_used_pct == 44.0
    assert u.memory_available is True


async def test_get_vm_utilisation_memory_unavailable_flag():
    handler = metrics_handler([1.0], memory_available_values=None)
    client = AzureClient("t", transport=httpx.MockTransport(handler))
    u = await get_vm_utilisation(client, "/subscriptions/s/vm-1")
    assert u.memory_available is False
    assert u.peak_memory_used_pct is None


async def test_enrich_vms_attaches_cpu_and_memory_fields():
    handler = metrics_handler([2.0, 4.0], max_values=[30.0, 45.0], memory_available_values=[90.0, 85.0])
    client = AzureClient("t", transport=httpx.MockTransport(handler))
    enriched = await enrich_vms_with_metrics(client, [dict(ARG_RUNNING_VM)])
    vm = enriched[0]
    assert vm["max_cpu"] == 45.0
    assert vm["peak_memory_used_pct"] == 15.0  # 100 - min(90,85)
    assert vm["memory_available"] is True
    assert vm["metric_window_days"] == 30


async def test_enrich_vms_empty_list():
    client = AzureClient("t", transport=httpx.MockTransport(metrics_handler([])))
    assert await enrich_vms_with_metrics(client, []) == []
