"""
Live Azure pricing via the Azure Retail Prices API (public, no auth).

Replaces the hard-coded 2023 price tables that used to live in `findings.py`.

Capabilities:
  - Region-aware pricing (armRegionName)
  - VM SKU consumption pricing (per-hour → per-month)
  - Reserved Instance pricing (1 Year / 3 Years), amortised to a monthly figure
  - Managed disk pricing by tier (size → P/E/S tier → per-disk monthly price)
  - Public IP pricing

Resilience:
  - Every query result is cached for 24h (configurable).
  - If the API is unreachable, we serve the last-known-good cached value (`get_stale`).
  - If there is no cache at all, a small STATIC_FALLBACK table keeps the assessment alive
    (findings flag reduced confidence when this path is hit — see Step 6).

The daily background refresh (`refresh_tracked`) re-fetches every filter currently in the
cache so warm keys never go stale under normal operation.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional

import httpx

from .cache import CacheBackend, InMemoryTTLCache
from .currency import from_usd

logger = logging.getLogger("cat.pricing")

RETAIL_PRICES_URL = "https://prices.azure.com/api/retail/prices"
RETAIL_API_VERSION = "2023-01-01-preview"  # supports savingsPlan + reservationTerm
HOURS_PER_MONTH = 730  # Azure's standard billing month
DEFAULT_TTL_SECONDS = 24 * 3600

# Azure managed-disk size → tier breakpoints (max GB for each tier).
# This is a stable Azure *spec* (not pricing), safe to hard-code. A disk is billed at the
# smallest tier whose capacity >= its provisioned size.
_DISK_TIER_BREAKPOINTS: List[tuple[int, int]] = [
    (4, 1), (8, 2), (16, 3), (32, 4), (64, 6), (128, 10), (256, 15),
    (512, 20), (1024, 30), (2048, 40), (4096, 50), (8192, 60),
    (16384, 70), (32767, 80),
]
_DISK_TIER_PREFIX = {
    "premium_lrs": "P", "premium_zrs": "P", "premiumv2_lrs": "P", "premium_v2_lrs": "P",
    "standardssd_lrs": "E", "standardssd_zrs": "E",
    "standard_lrs": "S",
}

# Last-resort fallback (per-GB/month, USD) used only when both the API and the cache miss.
# Deliberately conservative; findings that rely on it are marked lower-confidence.
STATIC_FALLBACK_DISK_PER_GB = {
    "ultrassd_lrs": 0.125, "premiumv2_lrs": 0.17, "premium_v2_lrs": 0.17,
    "premium_lrs": 0.135, "standardssd_zrs": 0.096, "standardssd_lrs": 0.075,
    "standard_lrs": 0.045,
}


class PricingUnavailableError(RuntimeError):
    """Raised when a price cannot be obtained from the API or any cache."""


def disk_tier_for_size(sku_name: str, size_gb: int) -> Optional[str]:
    """Map a managed-disk SKU + provisioned size to its Azure billing tier (e.g. 'P10').

    Returns None for SKUs billed per-GB rather than per-tier (e.g. UltraSSD).
    """
    prefix = _DISK_TIER_PREFIX.get((sku_name or "").lower())
    if prefix is None:
        return None
    for max_gb, tier_num in _DISK_TIER_BREAKPOINTS:
        if size_gb <= max_gb:
            return f"{prefix}{tier_num}"
    return f"{prefix}80"


class PricingEngine:
    """Async client for the Azure Retail Prices API with caching + fallback."""

    def __init__(
        self,
        cache: Optional[CacheBackend] = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        currency: str = "USD",
        transport: Optional[httpx.AsyncBaseTransport] = None,
        timeout: float = 30.0,
    ) -> None:
        self._cache = cache or InMemoryTTLCache()
        self._ttl = ttl_seconds
        self._currency = currency
        self._transport = transport  # injected in tests; None → real network
        self._timeout = timeout

    # ── Low-level query ────────────────────────────────────────────────────────

    async def query(self, filter_str: str) -> List[Dict]:
        """Return all price items matching an OData `$filter`, cached + paginated."""
        key = self._cache_key(filter_str)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        try:
            items = await self._fetch_all(filter_str)
            self._cache.set(key, items, self._ttl)
            return items
        except httpx.HTTPError as exc:
            stale = self._cache.get_stale(key)
            if stale is not None:
                logger.warning(
                    "Retail Prices API failed (%s); serving last-known-good for filter=%s",
                    exc, filter_str,
                )
                return stale
            logger.error("Retail Prices API failed, no cache for filter=%s: %s", filter_str, exc)
            raise PricingUnavailableError(str(exc)) from exc

    def _cache_key(self, filter_str: str) -> str:
        return f"retail:{self._currency}:{filter_str}"

    async def _fetch_all(self, filter_str: str) -> List[Dict]:
        items: List[Dict] = []
        params = {
            "api-version": RETAIL_API_VERSION,
            "$filter": filter_str,
            "currencyCode": self._currency,
        }
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            next_url: Optional[str] = None
            while True:
                if next_url is None:
                    resp = await client.get(RETAIL_PRICES_URL, params=params)
                else:
                    # NextPageLink is a fully-formed URL with the query embedded.
                    resp = await client.get(next_url)
                resp.raise_for_status()
                data = resp.json()
                items.extend(data.get("Items", []))
                next_url = data.get("NextPageLink")
                if not next_url:
                    break
        return items

    # ── VM pricing ─────────────────────────────────────────────────────────────

    async def get_vm_hourly_price(self, region: str, arm_sku_name: str) -> Optional[float]:
        """Cheapest Linux pay-as-you-go per-hour price for a VM SKU in a region."""
        filter_str = (
            "serviceName eq 'Virtual Machines' "
            f"and armRegionName eq '{region}' "
            f"and armSkuName eq '{arm_sku_name}' "
            "and priceType eq 'Consumption'"
        )
        items = await self.query(filter_str)
        candidates = [
            it.get("retailPrice")
            for it in items
            if it.get("type") == "Consumption"
            and it.get("retailPrice")
            and "Spot" not in (it.get("meterName") or "")
            and "Low Priority" not in (it.get("meterName") or "")
            and "Windows" not in (it.get("productName") or "")
        ]
        return min(candidates) if candidates else None

    async def get_vm_monthly_price(self, region: str, arm_sku_name: str) -> Optional[float]:
        hourly = await self.get_vm_hourly_price(region, arm_sku_name)
        return round(hourly * HOURS_PER_MONTH, 2) if hourly is not None else None

    async def get_vm_windows_monthly_price(self, region: str, arm_sku_name: str) -> Optional[float]:
        """Monthly price for the WINDOWS pay-as-you-go image (compute + Windows licence).

        Used for Azure Hybrid Benefit: AHB savings ≈ Windows price − Linux price = the licence cost.
        """
        filter_str = (
            "serviceName eq 'Virtual Machines' "
            f"and armRegionName eq '{region}' "
            f"and armSkuName eq '{arm_sku_name}' "
            "and priceType eq 'Consumption'"
        )
        items = await self.query(filter_str)
        candidates = [
            it.get("retailPrice")
            for it in items
            if it.get("type") == "Consumption"
            and it.get("retailPrice")
            and "Spot" not in (it.get("meterName") or "")
            and "Low Priority" not in (it.get("meterName") or "")
            and "Windows" in (it.get("productName") or "")
        ]
        if not candidates:
            return None
        return round(min(candidates) * HOURS_PER_MONTH, 2)

    async def get_vm_savings_plan_monthly_price(
        self, region: str, arm_sku_name: str, term: str = "1 Year"
    ) -> Optional[float]:
        """Compute Savings Plan monthly rate for a VM SKU, read from the retail item's `savingsPlan`.

        The Retail Prices API (preview) attaches a `savingsPlan: [{term, retailPrice}]` array (hourly
        rates) to consumption items. Returns None when the data isn't present for the SKU.
        """
        filter_str = (
            "serviceName eq 'Virtual Machines' "
            f"and armRegionName eq '{region}' "
            f"and armSkuName eq '{arm_sku_name}' "
            "and priceType eq 'Consumption'"
        )
        items = await self.query(filter_str)
        for it in items:
            if it.get("type") != "Consumption" or "Windows" in (it.get("productName") or ""):
                continue
            if "Spot" in (it.get("meterName") or "") or "Low Priority" in (it.get("meterName") or ""):
                continue
            for sp in it.get("savingsPlan") or []:
                if sp.get("term") == term and sp.get("retailPrice"):
                    return round(float(sp["retailPrice"]) * HOURS_PER_MONTH, 2)
        return None

    async def get_vm_reserved_monthly_price(
        self, region: str, arm_sku_name: str, term: str = "1 Year"
    ) -> Optional[float]:
        """Reserved Instance price amortised to a monthly figure.

        Reservation `retailPrice` is the total upfront cost for the whole term, so we divide
        by the number of months in the term (12 or 36).
        """
        months = 12 if term == "1 Year" else 36 if term == "3 Years" else None
        if months is None:
            raise ValueError("term must be '1 Year' or '3 Years'")
        filter_str = (
            "serviceName eq 'Virtual Machines' "
            f"and armRegionName eq '{region}' "
            f"and armSkuName eq '{arm_sku_name}' "
            "and priceType eq 'Reservation' "
            f"and reservationTerm eq '{term}'"
        )
        items = await self.query(filter_str)
        totals = [
            it.get("retailPrice")
            for it in items
            if it.get("reservationTerm") == term
            and it.get("retailPrice")
            and "Windows" not in (it.get("productName") or "")
        ]
        if not totals:
            return None
        return round(min(totals) / months, 2)

    # ── Managed disk pricing ───────────────────────────────────────────────────

    async def get_managed_disk_monthly_price(
        self, region: str, sku_name: str, size_gb: int
    ) -> Optional[float]:
        """Per-disk monthly price for a managed disk, by tier where applicable."""
        tier = disk_tier_for_size(sku_name, size_gb)
        redundancy = "ZRS" if (sku_name or "").lower().endswith("zrs") else "LRS"

        if tier is not None:
            filter_str = (
                "serviceName eq 'Storage' "
                f"and armRegionName eq '{region}' "
                f"and skuName eq '{tier} {redundancy}'"
            )
            try:
                items = await self.query(filter_str)
            except PricingUnavailableError:
                items = []
            prices = [
                it.get("retailPrice")
                for it in items
                if it.get("retailPrice")
                and "Disk" in (it.get("meterName") or "")
                and "/Month" in (it.get("unitOfMeasure") or "1/Month")
            ]
            if prices:
                return round(min(prices), 2)

        # Fallback: per-GB static estimate (USD, lower confidence) → billing currency.
        per_gb = STATIC_FALLBACK_DISK_PER_GB.get((sku_name or "").lower())
        if per_gb is not None:
            logger.info("Using static per-GB fallback for disk sku=%s size=%s", sku_name, size_gb)
            return from_usd(round(size_gb * per_gb, 2), self._currency)
        return None

    # ── Public IP pricing ──────────────────────────────────────────────────────

    async def get_public_ip_monthly_price(self, region: str, sku: str = "Standard") -> Optional[float]:
        filter_str = (
            "serviceName eq 'Virtual Network' "
            f"and armRegionName eq '{region}' "
            f"and meterName eq '{sku} IP Address Hours'"
        )
        try:
            items = await self.query(filter_str)
        except PricingUnavailableError:
            items = []
        hourly = [it.get("retailPrice") for it in items if it.get("retailPrice")]
        if hourly:
            return round(min(hourly) * HOURS_PER_MONTH, 2)
        # Static fallback (USD ≈ $3.65 Standard / $2.88 Basic) → billing currency.
        return from_usd(3.65 if sku.lower() == "standard" else 2.88, self._currency)

    # ── Maintenance ────────────────────────────────────────────────────────────

    async def refresh_tracked(self) -> int:
        """Re-fetch every filter currently in the cache. Returns count refreshed.

        Called by the daily background refresh so warm keys never expire in practice.
        Reconstructs the OData filter from the cache key (`retail:<currency>:<filter>`).
        """
        refreshed = 0
        for key in self._cache.keys():
            parts = key.split(":", 2)
            if len(parts) != 3 or parts[0] != "retail":
                continue
            filter_str = parts[2]
            try:
                items = await self._fetch_all(filter_str)
                self._cache.set(key, items, self._ttl)
                refreshed += 1
            except httpx.HTTPError as exc:
                logger.warning("Daily refresh failed for %s: %s", filter_str, exc)
        return refreshed


# ── Process-wide singleton + daily refresh loop ─────────────────────────────────

_engine_singleton: Optional[PricingEngine] = None


def get_pricing_engine(currency: Optional[str] = None) -> PricingEngine:
    """Return a PricingEngine for `currency` (default from settings).

    All currencies share ONE process-wide cache backend — the cache key already namespaces by
    currency (`retail:<currency>:<filter>`), so USD and CAD prices coexist without collision and a
    per-assessment currency doesn't spin up a fresh cache each time.
    """
    global _engine_singleton
    from ..config import settings
    if _engine_singleton is None:
        _engine_singleton = PricingEngine(
            ttl_seconds=settings.pricing_cache_ttl_seconds,
            currency=settings.pricing_currency,
        )
    want = (currency or settings.pricing_currency).upper()
    if want == _engine_singleton._currency.upper():
        return _engine_singleton
    return PricingEngine(
        cache=_engine_singleton._cache,
        ttl_seconds=settings.pricing_cache_ttl_seconds,
        currency=want,
    )


async def daily_refresh_loop(engine: PricingEngine, interval_seconds: int = DEFAULT_TTL_SECONDS) -> None:
    """Background loop: re-fetch cached prices once per interval. Never raises."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            count = await engine.refresh_tracked()
            logger.info("Daily pricing refresh complete: %d filters refreshed", count)
        except Exception as exc:  # defensive: a refresh error must not kill the loop
            logger.warning("Daily pricing refresh error: %s", exc)
