"""Unit + integration tests for the Resource Graph KQL layer (Step 3)."""
from __future__ import annotations

import httpx

from app.services.azure_client import AzureClient
from app.services.inventory import collect_inventory
from app.services import kql
from tests.azure_mocks import (
    ARG_ORPHANED_IP,
    ARG_RUNNING_VM,
    ARG_UNATTACHED_DISK,
    resource_graph_paginated_handler,
    resource_graph_router_handler,
)


# ── KQL builders push filtering server-side ─────────────────────────────────────

def test_unattached_disks_query_filters_server_side():
    q = kql.unattached_disks_query()
    assert "microsoft.compute/disks" in q
    assert "== 'Unattached'" in q  # predicate is in the KQL, not in Python


def test_orphaned_public_ips_query_filters_server_side():
    q = kql.orphaned_public_ips_query()
    assert "publicipaddresses" in q
    assert "isnull(properties.ipConfiguration)" in q


def test_idle_app_service_plans_query_filters_server_side():
    q = kql.idle_app_service_plans_query()
    assert "serverfarms" in q
    assert "numberOfSites) == 0" in q


def test_deallocated_vms_query_filters_server_side():
    q = kql.deallocated_vms_query()
    assert "virtualmachines" in q
    assert "'VM deallocated'" in q


def test_paused_sql_databases_query_filters_server_side():
    q = kql.paused_sql_databases_query()
    assert "servers/databases" in q
    assert "'Paused'" in q


def test_stopped_sql_managed_instances_query_filters_server_side():
    q = kql.stopped_sql_managed_instances_query()
    assert "managedinstances" in q
    assert "'Stopped'" in q


def test_running_vms_candidate_query():
    q = kql.running_vms_query()
    assert "'VM running'" in q


def test_registry_has_all_buckets():
    buckets = set(kql.filtered_inventory_queries())
    assert buckets == {
        "unattached_disks", "orphaned_public_ips", "idle_app_service_plans",
        "deallocated_vms", "paused_sql_databases", "stopped_sql_managed_instances",
        "running_vms",
        # broader coverage — cost-bearing only
        "orphaned_snapshots", "empty_load_balancers", "idle_nat_gateways", "bastion_hosts",
        # commitments / licensing / backup
        "windows_vms_without_ahb", "geo_redundant_vaults",
    }


def test_broad_coverage_queries_filter_server_side():
    assert "microsoft.compute/snapshots" in kql.orphaned_snapshots_query()
    assert "loadbalancers" in kql.empty_load_balancers_query()
    assert "natgateways" in kql.idle_nat_gateways_query()
    assert "bastionhosts" in kql.bastion_hosts_query()
    assert "'Windows'" in kql.windows_vms_without_ahb_query()
    assert "Windows_Server" in kql.windows_vms_without_ahb_query()
    assert "recoveryservices/vaults" in kql.geo_redundant_vaults_query()


def test_all_resources_summary_excludes_child_extensions():
    q = kql.all_resources_summary_query()
    assert "summarize" in q and "by type" in q
    assert "!endswith '/extensions'" in q  # child resources excluded to match the portal count


# ── ARG pagination (1000-row cap handling) ──────────────────────────────────────

async def test_query_resource_graph_follows_skip_token():
    counter = [0]
    page1 = [{"id": f"r{i}"} for i in range(1000)]
    page2 = [{"id": "r1000"}]
    handler = resource_graph_paginated_handler([page1, page2], request_counter=counter)
    client = AzureClient("fake-token", transport=httpx.MockTransport(handler))

    rows = await client.query_resource_graph(["sub-1"], "Resources | take 5000")

    assert counter[0] == 2          # two pages fetched via $skipToken
    assert len(rows) == 1001        # merged


# ── collect_inventory dispatches every bucket ───────────────────────────────────

async def test_collect_inventory_routes_and_returns_buckets():
    handler = resource_graph_router_handler({
        "== 'Unattached'": [ARG_UNATTACHED_DISK],
        "isnull(properties.ipConfiguration)": [ARG_ORPHANED_IP],
        "'VM running'": [ARG_RUNNING_VM],
    })
    client = AzureClient("fake-token", transport=httpx.MockTransport(handler))

    inventory, errors = await collect_inventory(client, ["sub-1"])

    assert errors == {}
    assert inventory["unattached_disks"] == [ARG_UNATTACHED_DISK]
    assert inventory["orphaned_public_ips"] == [ARG_ORPHANED_IP]
    assert inventory["running_vms"] == [ARG_RUNNING_VM]
    # Buckets with no matching rows come back empty, not missing.
    assert inventory["paused_sql_databases"] == []


async def test_collect_inventory_isolates_failing_bucket():
    # The disks query fails; every other bucket must still succeed.
    handler = resource_graph_router_handler(
        {"isnull(properties.ipConfiguration)": [ARG_ORPHANED_IP]},
        fail_markers=["== 'Unattached'"],
    )
    client = AzureClient("fake-token", transport=httpx.MockTransport(handler))

    inventory, errors = await collect_inventory(client, ["sub-1"])

    assert "unattached_disks" in errors            # failure surfaced
    assert inventory["unattached_disks"] == []     # but did not crash the run
    assert inventory["orphaned_public_ips"] == [ARG_ORPHANED_IP]
