"""
Inventory collection via Azure Resource Graph.

Filtering is now performed server-side in ARG (see kql.py). This module simply dispatches
every registered query in parallel and returns the rows Azure already narrowed down, keyed by
bucket name. A failed query for one bucket does not fail the whole assessment — that bucket is
returned empty and the failure is surfaced via the returned `errors` map.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Tuple

from .azure_client import AzureClient
from .kql import filtered_inventory_queries

logger = logging.getLogger("cat.inventory")


async def collect_inventory(
    client: AzureClient, subscription_ids: List[str]
) -> Tuple[Dict[str, List[Dict]], Dict[str, str]]:
    """Run all server-side-filtered ARG queries in parallel.

    Returns `(inventory, errors)` where `inventory[bucket]` is the list of rows Azure returned
    for that bucket, and `errors[bucket]` holds the error message for any bucket that failed.
    """
    queries = filtered_inventory_queries()
    tasks = {
        bucket: client.query_resource_graph(subscription_ids, query)
        for bucket, query in queries.items()
    }
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)

    inventory: Dict[str, List[Dict]] = {}
    errors: Dict[str, str] = {}
    for bucket, result in zip(tasks.keys(), results):
        if isinstance(result, list):
            inventory[bucket] = result
        else:
            inventory[bucket] = []
            errors[bucket] = str(result)
            logger.warning("Resource Graph query failed for bucket '%s': %s", bucket, result)

    return inventory, errors
