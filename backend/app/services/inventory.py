from typing import Dict, List
from .azure_client import AzureClient


async def collect_inventory(client: AzureClient, subscription_ids: List[str]) -> Dict[str, List[Dict]]:
    """Run all Resource Graph queries in parallel and return typed inventory buckets."""
    import asyncio

    queries = {
        "virtual_machines": """Resources
            | where type == 'microsoft.compute/virtualmachines'
            | extend powerState = tostring(properties.extended.instanceView.powerState.displayStatus)
            | project id, name, subscriptionId, resourceGroup, location,
                      vmSize=tostring(properties.hardwareProfile.vmSize),
                      powerState, tags""",

        "managed_disks": """Resources
            | where type == 'microsoft.compute/disks'
            | project id, name, subscriptionId, resourceGroup, location,
                      diskState=tostring(properties.diskState),
                      skuName=tostring(sku.name),
                      diskSizeGB=toint(properties.diskSizeGB),
                      timeCreated=tostring(properties.timeCreated), tags""",

        "public_ips": """Resources
            | where type == 'microsoft.network/publicipaddresses'
            | extend associated = tostring(properties.ipConfiguration.id)
            | project id, name, subscriptionId, resourceGroup, location,
                      ipAddress=tostring(properties.ipAddress),
                      associated,
                      allocationMethod=tostring(properties.publicIPAllocationMethod),
                      skuName=tostring(sku.name), tags""",

        "app_service_plans": """Resources
            | where type == 'microsoft.web/serverfarms'
            | project id, name, subscriptionId, resourceGroup, location,
                      skuName=tostring(sku.name),
                      tier=tostring(sku.tier),
                      numberOfSites=toint(properties.numberOfSites),
                      status=tostring(properties.status), tags""",

        "sql_databases": """Resources
            | where type == 'microsoft.sql/servers/databases'
            | where name != 'master'
            | project id, name, subscriptionId, resourceGroup, location,
                      skuName=tostring(sku.name),
                      tier=tostring(sku.tier),
                      status=tostring(properties.status),
                      maxSizeBytes=tolong(properties.maxSizeBytes),
                      creationDate=tostring(properties.creationDate), tags""",

        "sql_managed_instances": """Resources
            | where type == 'microsoft.sql/managedinstances'
            | project id, name, subscriptionId, resourceGroup, location,
                      skuName=tostring(sku.name),
                      tier=tostring(sku.tier),
                      vCores=toint(properties.vCores),
                      state=tostring(properties.state), tags""",

        "recovery_vaults": """Resources
            | where type == 'microsoft.recoveryservices/vaults'
            | project id, name, subscriptionId, resourceGroup, location,
                      skuName=tostring(sku.name), tags""",

        "cosmos_accounts": """Resources
            | where type == 'microsoft.documentdb/databaseaccounts'
            | project id, name, subscriptionId, resourceGroup, location,
                      kind, tags""",

        "mysql_servers": """Resources
            | where type in ('microsoft.dbformysql/servers','microsoft.dbformysql/flexibleservers')
            | project id, name, subscriptionId, resourceGroup, location,
                      skuName=tostring(sku.name),
                      tier=tostring(sku.tier), tags""",

        "storage_accounts": """Resources
            | where type == 'microsoft.storage/storageaccounts'
            | project id, name, subscriptionId, resourceGroup, location,
                      kind, skuName=tostring(sku.name),
                      accessTier=tostring(properties.accessTier), tags""",
    }

    tasks = {
        key: client.query_resource_graph(subscription_ids, q)
        for key, q in queries.items()
    }
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    inventory: Dict[str, List[Dict]] = {}
    for key, result in zip(tasks.keys(), results):
        inventory[key] = result if isinstance(result, list) else []

    return inventory
