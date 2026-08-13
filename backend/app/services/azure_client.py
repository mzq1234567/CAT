import asyncio
import logging
import weakref
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit
import httpx

from .collection import RetryStats
from .resilience import CircuitBreaker, retry_request

logger = logging.getLogger("cat.azure")


# ── Concurrency: exactly TWO documented limits, no hidden per-stage caps ──────────────
# 1. PER-RUN cap  (AzureClient._semaphore, settings.azure_max_concurrency, default 8): bounds a single
#    assessment's in-flight Azure requests. Azure Resource Graph throttling is PER-TENANT (~15 queries
#    /5s), and one assessment targets one tenant's subscriptions, so this is the limit that actually
#    protects a tenant's rate bucket — 8 keeps well under the limit with headroom, far faster than serial.
# 2. PROCESS-WIDE cap  (this module, settings.azure_global_max_concurrency, default 24 = 3×per-run):
#    bounds TOTAL simultaneous Azure requests across ALL concurrently-running assessments, so N runs can't
#    multiply the per-run cap into uncontrolled aggregate outbound traffic. ~3 assessments run full-speed;
#    beyond that they share the global budget. Both are configurable via env.
# Every request acquires BOTH (process-wide, then per-run) — there is no third/hidden concurrency limit.
_GLOBAL_SEMAPHORES: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _global_semaphore() -> asyncio.Semaphore:
    """The process-wide Azure-concurrency semaphore for the CURRENT event loop.

    Cached per running loop so a fresh test loop gets a correctly-bound semaphore, while a real process
    (one loop) shares ONE semaphore across every concurrent assessment. Resizes if the setting changes.
    """
    from ..config import settings
    loop = asyncio.get_running_loop()
    size = max(1, settings.azure_global_max_concurrency)
    entry = _GLOBAL_SEMAPHORES.get(loop)
    if entry is None or entry[1] != size:
        entry = (asyncio.Semaphore(size), size)
        _GLOBAL_SEMAPHORES[loop] = entry
    return entry[0]


def _label_for(method: str, url: str) -> str:
    """Concise, secret-free label for a request, for retry logs (never includes the token/headers).

    Uses the provider/type tail of the ARM path (e.g. "GET Microsoft.ResourceGraph/resources"), which
    identifies the API/resource being queried without dumping the full resource id.
    """
    path = urlsplit(url).path
    tail = path.split("/providers/")[-1] if "/providers/" in path else path
    return f"{method} {tail[:80]}"

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
        max_concurrency: int = 8, stats: Optional[RetryStats] = None,
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
        # PER-RUN concurrency cap (see module header). Every outbound request also passes the PROCESS-WIDE
        # semaphore, so no caller's fan-out (e.g. ~20 parallel Resource Graph queries, a metrics sweep)
        # exceeds this per assessment, and no set of concurrent assessments exceeds the global budget. The
        # slot is held only for the HTTP round-trip; retry backoff waits release it.
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        # Run-level retry/throttle counters (feeds the data-quality report). Shared, so all calls tally
        # into one place the pipeline can read after the run.
        self.stats = stats if stats is not None else RetryStats()

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, transport=self._transport)

    async def _send(
        self, client: httpx.AsyncClient, method: str, url: str,
        *, max_retries: Optional[int] = None, use_breaker: bool = True,
        label: Optional[str] = None, **kwargs,
    ) -> httpx.Response:
        """Send a request with bounded concurrency + retry/backoff on 429/5xx and (optionally) the
        shared circuit breaker.

        Cost Management + Consumption are throttled far more aggressively than the fast ARM calls and
        return a `Retry-After`, so those callers pass a higher `max_retries` and `use_breaker=False`
        to (a) wait out throttling instead of failing, and (b) stay isolated from the shared breaker
        so a busy metrics run can't fail-fast the billing query (and vice-versa).
        """
        async def do() -> httpx.Response:
            # Acquire the process-wide budget first, then this run's slot — held only for the round-trip,
            # released before any retry backoff sleep. Consistent order (global→per-run) → no deadlock.
            async with _global_semaphore():
                async with self._semaphore:
                    return await client.request(method, url, headers=self._headers, **kwargs)

        return await retry_request(
            do,
            max_retries=self._max_retries if max_retries is None else max_retries,
            base_delay=self._base_delay,
            breaker=self._breaker if use_breaker else None,
            label=label or _label_for(method, url),
            stats=self.stats,
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

        Look-back window: Azure evaluates steadiness over 7/30/60 days and defaults the API to the
        STRICTEST (`Last7Days`), where a single day of shutdown suppresses the recommendation. We
        explicitly request `Last30Days` — a month of steady usage is the right basis for a 1–3yr
        commitment and is far more forgiving of a brief restart, so real steadily-run workloads aren't
        dropped. (Ref: Microsoft "Reserved instance purchase recommendations" — lookBackPeriod.)
        """
        lookback = "properties/lookBackPeriod eq 'Last30Days'"
        recs, why = await self._fetch_reservation_recs(
            subscription_id, f"properties/scope eq '{scope}' and {lookback}")
        if not recs and scope == "Single":
            recs, why = await self._fetch_reservation_recs(subscription_id, lookback)  # any scope
        logger.info("Reservation recommendations for %s: %d returned (30-day look-back; %s).",
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
