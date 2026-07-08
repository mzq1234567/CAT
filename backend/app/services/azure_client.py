from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
import httpx

ARM_BASE = "https://management.azure.com"
SUBSCRIPTIONS_API = "2022-12-01"
ADVISOR_API = "2023-01-01"
RESOURCE_GRAPH_API = "2021-03-01"
METRICS_API = "2023-10-01"


class AzureClient:
    def __init__(self, token: str):
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def get_subscriptions(self) -> List[Dict]:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{ARM_BASE}/subscriptions?api-version={SUBSCRIPTIONS_API}",
                headers=self._headers,
            )
            r.raise_for_status()
            return r.json().get("value", [])

    async def get_advisor_cost_recommendations(self, subscription_id: str) -> List[Dict]:
        results: List[Dict] = []
        url = (
            f"{ARM_BASE}/subscriptions/{subscription_id}/providers/Microsoft.Advisor"
            f"/recommendations?api-version={ADVISOR_API}&$filter=Category eq 'Cost'"
        )
        async with httpx.AsyncClient(timeout=60) as client:
            while url:
                r = await client.get(url, headers=self._headers)
                if r.status_code in (403, 404):
                    break
                r.raise_for_status()
                data = r.json()
                results.extend(data.get("value", []))
                url = data.get("nextLink")
        return results

    async def query_resource_graph(
        self, subscription_ids: List[str], query: str
    ) -> List[Dict]:
        results: List[Dict] = []
        skip_token: Optional[str] = None

        async with httpx.AsyncClient(timeout=60) as client:
            while True:
                body: Dict[str, Any] = {
                    "subscriptions": subscription_ids,
                    "query": query,
                    "options": {"$top": 1000},
                }
                if skip_token:
                    body["options"]["$skipToken"] = skip_token

                r = await client.post(
                    f"{ARM_BASE}/providers/Microsoft.ResourceGraph/resources"
                    f"?api-version={RESOURCE_GRAPH_API}",
                    headers=self._headers,
                    json=body,
                )
                r.raise_for_status()
                data = r.json()
                results.extend(data.get("data", []))
                skip_token = data.get("$skipToken")
                if not skip_token:
                    break

        return results
