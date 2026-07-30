import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
import httpx

from .resilience import CircuitBreaker, retry_request

logger = logging.getLogger("cat.azure")

ARM_BASE = "https://management.azure.com"
SUBSCRIPTIONS_API = "2022-12-01"
ADVISOR_API = "2023-01-01"
RESOURCE_GRAPH_API = "2021-03-01"
METRICS_API = "2023-10-01"
COST_MANAGEMENT_API = "2023-11-01"
CONSUMPTION_API = "2023-05-01"


class AzureClient:
    def __init__(
        self, token: str, transport: Optional[httpx.AsyncBaseTransport] = None,
        max_retries: int = 4, base_delay: float = 0.5,
        breaker: Optional[CircuitBreaker] = None,
    ):
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        # Injected in tests (httpx.MockTransport); None → real network.
        self._transport = transport
        self._max_retries = max_retries
        self._base_delay = base_delay
        # One breaker per client instance (per assessment run) so hard throttling fails fast.
        self._breaker = breaker if breaker is not None else CircuitBreaker(name="azure-arm")

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, transport=self._transport)

    async def _send(
        self, client: httpx.AsyncClient, method: str, url: str,
        *, max_retries: Optional[int] = None, use_breaker: bool = True, **kwargs,
    ) -> httpx.Response:
        """Send a request with retry/backoff on 429/503 and (optionally) the shared circuit breaker.

        Cost Management + Consumption are throttled far more aggressively than the fast ARM calls and
        return a `Retry-After`, so those callers pass a higher `max_retries` and `use_breaker=False`
        to (a) wait out throttling instead of failing, and (b) stay isolated from the shared breaker
        so a busy metrics run can't fail-fast the billing query (and vice-versa).
        """
        async def do() -> httpx.Response:
            return await client.request(method, url, headers=self._headers, **kwargs)

        return await retry_request(
            do,
            max_retries=self._max_retries if max_retries is None else max_retries,
            base_delay=self._base_delay,
            breaker=self._breaker if use_breaker else None,
        )

    async def get_subscriptions(self) -> List[Dict]:
        async with self._client(30) as client:
            r = await self._send(
                client, "GET", f"{ARM_BASE}/subscriptions?api-version={SUBSCRIPTIONS_API}",
            )
            r.raise_for_status()
            return r.json().get("value", [])

    async def get_tenant_display_name(self, tenant_id: Optional[str] = None) -> Optional[str]:
        """The Azure AD tenant's friendly name (the client name for the report cover).

        Returns the display name for `tenant_id` when given, else the first tenant. None on any
        failure / no access — the report falls back to the tenant GUID or subscription name.
        """
        try:
            async with self._client(30) as client:
                r = await self._send(
                    client, "GET", f"{ARM_BASE}/tenants?api-version={SUBSCRIPTIONS_API}",
                )
                if r.status_code in (403, 404):
                    return None
                r.raise_for_status()
                tenants = r.json().get("value", [])
        except Exception:  # noqa: BLE001 — cosmetic metadata, never fail the assessment
            return None
        if tenant_id:
            for t in tenants:
                if (t.get("tenantId") or "").lower() == tenant_id.lower():
                    return t.get("displayName") or t.get("defaultDomain")
        return (tenants[0].get("displayName") or tenants[0].get("defaultDomain")) if tenants else None

    async def get_advisor_cost_recommendations(self, subscription_id: str) -> List[Dict]:
        results: List[Dict] = []
        url = (
            f"{ARM_BASE}/subscriptions/{subscription_id}/providers/Microsoft.Advisor"
            f"/recommendations?api-version={ADVISOR_API}&$filter=Category eq 'Cost'"
        )
        async with self._client(60) as client:
            while url:
                r = await self._send(client, "GET", url)
                if r.status_code in (403, 404):
                    break
                r.raise_for_status()
                data = r.json()
                results.extend(data.get("value", []))
                url = data.get("nextLink")
        return results

    async def get_reservation_recommendations(
        self, subscription_id: str, scope: str = "Single",
    ) -> List[Dict]:
        """Azure's own usage-based reservation purchase recommendations (Consumption API).

        Authoritative: Azure simulates your actual hourly usage over 7/30/60 days at your real
        (negotiated) prices, excludes reservations you already own, and returns the quantity that
        maximises savings — per SKU/region, for both 1-year and 3-year terms. Covers VMs, SQL,
        Cosmos, MySQL/PostgreSQL, App Service, Managed Disk and more. 403/404 (no Cost Management
        access) → [] rather than fatal, exactly like the Advisor + Cost Management calls.

        Prefers `Single` (subscription-specific) recommendations; if none come back, retries WITHOUT a
        scope filter so tenants that only surface `Shared`-scope recs still get results. Outcomes are
        logged (status/count/reason) so a run that returns nothing is diagnosable — Azure legitimately
        returns none when resources aren't run steadily enough to justify a reservation.
        """
        recs, why = await self._fetch_reservation_recs(subscription_id, f"properties/scope eq '{scope}'")
        if not recs and scope == "Single":
            recs, why = await self._fetch_reservation_recs(subscription_id, None)  # any scope
        logger.info("Reservation recommendations for %s: %d returned (%s).",
                    subscription_id, len(recs), why)
        return recs

    async def _fetch_reservation_recs(self, subscription_id: str, filter_expr: Optional[str]):
        """Fetch reservationRecommendations; returns (items, reason) where reason explains an empty result."""
        results: List[Dict] = []
        url: Optional[str] = (
            f"{ARM_BASE}/subscriptions/{subscription_id}/providers/Microsoft.Consumption"
            f"/reservationRecommendations?api-version={CONSUMPTION_API}"
            + (f"&$filter={filter_expr}" if filter_expr else "")
        )
        reason = "ok"
        async with self._client(90) as client:
            while url:
                r = await self._send(client, "GET", url, max_retries=8, use_breaker=False)
                if r.status_code in (403, 404):
                    return [], f"no access/not-enabled (HTTP {r.status_code})"
                if r.status_code == 429:
                    return results, "throttled (HTTP 429) after retries"
                if r.status_code >= 400:
                    return results, f"HTTP {r.status_code}: {r.text[:200]}"
                data = r.json()
                results.extend(data.get("value", []))
                url = data.get("nextLink")
        if not results:
            reason = "Azure returned no recommendations (usage too low/spiky to justify a reservation)"
        return results, reason

    async def query_resource_graph(
        self, subscription_ids: List[str], query: str
    ) -> List[Dict]:
        """Run a KQL query against Azure Resource Graph.

        Handles ARG's 1000-row page cap by following `$skipToken` until exhausted.
        Filtering is expected to be expressed *in the KQL* (server-side) — see kql.py.
        """
        results: List[Dict] = []
        skip_token: Optional[str] = None

        async with self._client(60) as client:
            while True:
                body: Dict[str, Any] = {
                    "subscriptions": subscription_ids,
                    "query": query,
                    "options": {"$top": 1000},
                }
                if skip_token:
                    body["options"]["$skipToken"] = skip_token

                r = await self._send(
                    client, "POST",
                    f"{ARM_BASE}/providers/Microsoft.ResourceGraph/resources"
                    f"?api-version={RESOURCE_GRAPH_API}",
                    json=body,
                )
                r.raise_for_status()
                data = r.json()
                results.extend(data.get("data", []))
                skip_token = data.get("$skipToken")
                if not skip_token:
                    break

        return results

    async def get_metric(
        self, resource_id: str, metric_name: str, days: int = 7,
        interval: str = "P1D", aggregation: str = "Average",
    ) -> List[float]:
        """Fetch an Azure Monitor metric time-series and return the non-null aggregated values.

        Used for utilisation-based findings (idle / oversized / RI candidates). Missing metrics
        (403/404, or a resource that emits nothing) return an empty list rather than failing.
        """
        end = datetime.utcnow()
        start = end - timedelta(days=days)
        timespan = f"{start.strftime('%Y-%m-%dT%H:%M:%SZ')}/{end.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        url = f"{ARM_BASE}{resource_id}/providers/microsoft.insights/metrics"
        params = {
            "api-version": METRICS_API,
            "metricnames": metric_name,
            "timespan": timespan,
            "interval": interval,
            "aggregation": aggregation,
        }
        agg_key = aggregation.lower()
        async with self._client(60) as client:
            r = await self._send(client, "GET", url, params=params)
            if r.status_code in (403, 404):
                return []
            r.raise_for_status()
            data = r.json()

        values: List[float] = []
        for metric in data.get("value", []):
            for series in metric.get("timeseries", []):
                for point in series.get("data", []):
                    v = point.get(agg_key)
                    if v is not None:
                        values.append(float(v))
        return values

    async def query_cost_management(self, subscription_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """POST a Cost Management query and return a merged {properties:{columns,rows}} payload.

        Follows `properties.nextLink` for paging. 403/404 (no cost access / not enabled) are treated
        as "no data" rather than fatal. Cost Management throttles hard, so this uses patient retries
        and is isolated from the shared circuit breaker (see `_send`); a final 429 (throttled even
        after backoff) raises so the caller can distinguish "throttled, retry" from "no access".
        """
        url: Optional[str] = (
            f"{ARM_BASE}/subscriptions/{subscription_id}/providers/Microsoft.CostManagement"
            f"/query?api-version={COST_MANAGEMENT_API}"
        )
        all_rows: List[List[Any]] = []
        columns: Optional[List[Dict]] = None

        async with self._client(90) as client:
            while url:
                r = await self._send(client, "POST", url, json=body, max_retries=8, use_breaker=False)
                if r.status_code in (403, 404):
                    break
                r.raise_for_status()  # a persistent 429 surfaces here → CostThrottled at the caller
                props = r.json().get("properties", {})
                if columns is None:
                    columns = props.get("columns", [])
                all_rows.extend(props.get("rows", []))
                url = props.get("nextLink")

        return {"properties": {"columns": columns or [], "rows": all_rows}}
