from fastapi import APIRouter, Depends
from typing import List

from ...api.dependencies import get_current_user
from ...models.schemas import SubscriptionResponse
from ...services.azure_client import AzureClient

router = APIRouter()


@router.get("/", response_model=List[SubscriptionResponse])
async def list_subscriptions(user: dict = Depends(get_current_user)):
    client = AzureClient(user["token"])
    subs = await client.get_subscriptions()
    return [
        SubscriptionResponse(
            id=s["subscriptionId"],
            display_name=s.get("displayName", s["subscriptionId"]),
            state=s.get("state", "Enabled"),
            tenant_id=s.get("tenantId", ""),
        )
        for s in subs
        if s.get("state") == "Enabled"
    ]
