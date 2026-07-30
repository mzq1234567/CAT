"""
KQL query builders for Azure Resource Graph — server-side filtering.

Previously the app fetched *all* resources of each type and filtered them in Python. Here,
the filtering predicate (unattached / orphaned / stopped / paused) is expressed as a KQL
`where` clause so Azure Resource Graph evaluates it server-side and returns only the rows we
care about. This cuts payload size dramatically and removes an entire class of client-side bugs.

ARG's 1000-row page cap is handled by the paginating client (AzureClient.query_resource_graph);
these builders are pure string factories and are trivially unit-testable.

Two families of query:
  * FILTERED  — the predicate fully determines a finding candidate (e.g. an unattached disk).
  * CANDIDATE — server-side filter narrows to resources that *may* be a finding but need a
                metrics cross-check (e.g. a running VM that might be idle or oversized). The
                metrics step (findings engine) makes the final call.
"""
from __future__ import annotations

from typing import Dict

# Power state is surfaced by ARG under the extended instance view.
_VM_POWER_STATE = "properties.extended.instanceView.powerState.displayStatus"


# ── FILTERED queries (predicate = finding candidate) ────────────────────────────

def unattached_disks_query() -> str:
    return f"""Resources
| where type == 'microsoft.compute/disks'
| where tostring(properties.diskState) == 'Unattached'
| project id, name, subscriptionId, resourceGroup, location,
          diskState = tostring(properties.diskState),
          skuName = tostring(sku.name),
          diskSizeGB = toint(properties.diskSizeGB),
          timeCreated = tostring(properties.timeCreated), tags"""


def orphaned_public_ips_query() -> str:
    # No IP configuration and not attached to a NAT gateway → not associated with anything.
    return """Resources
| where type == 'microsoft.network/publicipaddresses'
| where isnull(properties.ipConfiguration) and isnull(properties.natGateway)
| project id, name, subscriptionId, resourceGroup, location,
          ipAddress = tostring(properties.ipAddress),
          allocationMethod = tostring(properties.publicIPAllocationMethod),
          skuName = tostring(sku.name), tags"""


def idle_app_service_plans_query() -> str:
    return """Resources
| where type == 'microsoft.web/serverfarms'
| where toint(properties.numberOfSites) == 0
| project id, name, subscriptionId, resourceGroup, location,
          skuName = tostring(sku.name),
          tier = tostring(sku.tier),
          numberOfSites = toint(properties.numberOfSites),
          capacity = toint(sku.capacity), tags"""


def deallocated_vms_query() -> str:
    return f"""Resources
| where type == 'microsoft.compute/virtualmachines'
| extend powerState = tostring({_VM_POWER_STATE})
| where powerState in ('VM deallocated', 'VM stopped')
| project id, name, subscriptionId, resourceGroup, location,
          vmSize = tostring(properties.hardwareProfile.vmSize),
          powerState, tags"""


def paused_sql_databases_query() -> str:
    return """Resources
| where type == 'microsoft.sql/servers/databases'
| where name != 'master'
| where tostring(properties.status) in ('Paused', 'Disabled', 'Offline', 'Inaccessible')
| project id, name, subscriptionId, resourceGroup, location,
          skuName = tostring(sku.name),
          tier = tostring(sku.tier),
          status = tostring(properties.status), tags"""


def stopped_sql_managed_instances_query() -> str:
    return """Resources
| where type == 'microsoft.sql/managedinstances'
| where tostring(properties.state) in ('Stopped', 'Failed', 'Inaccessible')
| project id, name, subscriptionId, resourceGroup, location,
          skuName = tostring(sku.name),
          tier = tostring(sku.tier),
          vCores = toint(properties.vCores),
          state = tostring(properties.state), tags"""


# ── CANDIDATE queries (need a metrics cross-check) ──────────────────────────────

def running_vms_query() -> str:
    """Running VMs — candidates for idle / oversized / RI, decided later via metrics."""
    return f"""Resources
| where type == 'microsoft.compute/virtualmachines'
| extend powerState = tostring({_VM_POWER_STATE})
| where powerState == 'VM running' or isempty(powerState)
| project id, name, subscriptionId, resourceGroup, location,
          vmSize = tostring(properties.hardwareProfile.vmSize),
          osType = tostring(properties.storageProfile.osDisk.osType),
          powerState, tags"""


# ── Broader coverage: orphaned / idle resources across many service types ────────
# These follow common ARG patterns for detecting resources that bill while doing nothing.
# Property paths for orphan-detection can vary by resource; treat as best-effort and verify
# against a real environment (documented in memory.md).

def orphaned_snapshots_query() -> str:
    return """Resources
| where type == 'microsoft.compute/snapshots'
| project id, name, subscriptionId, resourceGroup, location,
          diskSizeGB = toint(properties.diskSizeGB),
          timeCreated = tostring(properties.timeCreated), tags"""


def empty_load_balancers_query() -> str:
    return """Resources
| where type == 'microsoft.network/loadbalancers'
| where tostring(sku.name) == 'Standard'
| where isnull(properties.backendAddressPools) or array_length(properties.backendAddressPools) == 0
| project id, name, subscriptionId, resourceGroup, location, skuName = tostring(sku.name), tags"""


def idle_nat_gateways_query() -> str:
    return """Resources
| where type == 'microsoft.network/natgateways'
| where isnull(properties.subnets) or array_length(properties.subnets) == 0
| project id, name, subscriptionId, resourceGroup, location, skuName = tostring(sku.name), tags"""


def bastion_hosts_query() -> str:
    return """Resources
| where type == 'microsoft.network/bastionhosts'
| project id, name, subscriptionId, resourceGroup, location, skuName = tostring(sku.name), tags"""


def windows_vms_without_ahb_query() -> str:
    """Running Windows VMs NOT using Azure Hybrid Benefit (licenseType != Windows_Server)."""
    return f"""Resources
| where type == 'microsoft.compute/virtualmachines'
| extend powerState = tostring({_VM_POWER_STATE})
| where tostring(properties.storageProfile.osDisk.osType) == 'Windows'
| where isnull(properties.licenseType) or tostring(properties.licenseType) != 'Windows_Server'
| where powerState == 'VM running' or isempty(powerState)
| project id, name, subscriptionId, resourceGroup, location,
          vmSize = tostring(properties.hardwareProfile.vmSize), powerState, tags"""


def geo_redundant_vaults_query() -> str:
    """Recovery Services vaults whose backup storage is geo-redundant (candidate for LRS)."""
    return """Resources
| where type == 'microsoft.recoveryservices/vaults'
| where tostring(properties.redundancySettings.standardTierStorageRedundancy) == 'GeoRedundant'
| project id, name, subscriptionId, resourceGroup, location, tags"""


def all_resources_summary_query() -> str:
    """Full inventory: top-level resources counted by type. Powers 'N resources / M types scanned'.

    Excludes child resources (e.g. `.../extensions` like the monitoring agent) that Resource Graph
    lists but the portal's resource view hides — so the count lines up with what you'd see there.
    """
    return """Resources
| where type !endswith '/extensions'
| summarize resourceCount = count() by type
| project type, resourceCount
| order by resourceCount desc"""


# ── Registry ────────────────────────────────────────────────────────────────────

def filtered_inventory_queries() -> Dict[str, str]:
    """All server-side-filtered ARG queries, keyed by inventory bucket name.

    Bucket names map directly onto finding categories consumed by the findings engine.
    """
    return {
        # Compute / data (existing)
        "unattached_disks": unattached_disks_query(),
        "orphaned_public_ips": orphaned_public_ips_query(),
        "idle_app_service_plans": idle_app_service_plans_query(),
        "deallocated_vms": deallocated_vms_query(),
        "paused_sql_databases": paused_sql_databases_query(),
        "stopped_sql_managed_instances": stopped_sql_managed_instances_query(),
        "running_vms": running_vms_query(),
        # Broader coverage — cost-bearing orphans/waste (new)
        "orphaned_snapshots": orphaned_snapshots_query(),
        "empty_load_balancers": empty_load_balancers_query(),
        "idle_nat_gateways": idle_nat_gateways_query(),
        "bastion_hosts": bastion_hosts_query(),
        # Commitments / licensing / backup
        "windows_vms_without_ahb": windows_vms_without_ahb_query(),
        "geo_redundant_vaults": geo_redundant_vaults_query(),
    }
