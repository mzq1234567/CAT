"""
RBAC enforcement (Step 7).

The tool uses delegated auth, so ARM only returns subscriptions the caller actually has a role on.
We use that as the authorization check: before assessing a subscription we confirm the caller has
at least Reader on it (i.e. it appears in their accessible subscription list). This prevents a user
from assessing a subscription id they merely guessed.
"""
from __future__ import annotations

from typing import List, Set

from fastapi import HTTPException

from ..services.azure_client import AzureClient


async def get_accessible_subscription_ids(client: AzureClient) -> Set[str]:
    subs = await client.get_subscriptions()
    return {s["subscriptionId"] for s in subs if s.get("state") == "Enabled"}


async def verify_subscription_access(client: AzureClient, requested: List[str]) -> None:
    """Raise 403 if the caller lacks access to any requested subscription."""
    accessible = await get_accessible_subscription_ids(client)
    missing = [s for s in requested if s not in accessible]
    if missing:
        raise HTTPException(
            status_code=403,
            detail=f"You do not have Reader access to subscription(s): {', '.join(missing)}",
        )
