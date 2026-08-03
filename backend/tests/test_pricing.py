"""Unit tests for the pricing engine (Step 2)."""
from __future__ import annotations

import httpx
import pytest

from app.services.cache import InMemoryTTLCache
from app.services.pricing import (
    HOURS_PER_MONTH,
    PricingEngine,
    PricingUnavailableError,
    disk_tier_for_size,
)
from tests.azure_mocks import retail_prices_handler


def make_engine(handler, ttl_seconds=86400, cache=None) -> PricingEngine:
    return PricingEngine(
        cache=cache or InMemoryTTLCache(),
        ttl_seconds=ttl_seconds,
        transport=httpx.MockTransport(handler),
    )


# ── disk_tier_for_size (pure) ──────────────────────────────────────────────────

@pytest.mark.parametrize(
    "sku, size, expected",
    [
        ("premium_lrs", 100, "P10"),     # 100 <= 128
        ("premium_lrs", 128, "P10"),
        ("premium_lrs", 129, "P15"),     # rolls to next tier
        ("premium_lrs", 1, "P1"),
        ("standardssd_lrs", 256, "E15"),
        ("standard_lrs", 1024, "S30"),
        ("standard_lrs", 50000, "S80"),  # above the top breakpoint
        ("ultrassd_lrs", 100, None),     # per-GB SKU, no tier
        ("unknown_sku", 100, None),
    ],
)
def test_disk_tier_for_size(sku, size, expected):
    assert disk_tier_for_size(sku, size) == expected


# ── VM pricing ─────────────────────────────────────────────────────────────────

async def test_vm_hourly_price_picks_cheapest_linux():
    engine = make_engine(retail_prices_handler())
    price = await engine.get_vm_hourly_price("eastus", "Standard_D2s_v3")
    # Cheapest non-Spot, non-Windows consumption price is 0.096 (Spot 0.019 excluded).
    assert price == 0.096


async def test_vm_monthly_price():
    engine = make_engine(retail_prices_handler())
    monthly = await engine.get_vm_monthly_price("eastus", "Standard_D2s_v3")
    assert monthly == round(0.096 * HOURS_PER_MONTH, 2)


async def test_vm_reserved_monthly_price_1yr_and_3yr():
    engine = make_engine(retail_prices_handler())
    one_yr = await engine.get_vm_reserved_monthly_price("eastus", "Standard_D2s_v3", "1 Year")
    three_yr = await engine.get_vm_reserved_monthly_price("eastus", "Standard_D2s_v3", "3 Years")
    assert one_yr == round(840.0 / 12, 2)     # 70.0
    assert three_yr == round(1800.0 / 36, 2)  # 50.0


async def test_vm_windows_monthly_price_for_ahb():
    engine = make_engine(retail_prices_handler())
    win = await engine.get_vm_windows_monthly_price("eastus", "Standard_D2s_v3")
    assert win == round(0.188 * HOURS_PER_MONTH, 2)  # Windows image (compute + licence)


async def test_vm_savings_plan_monthly_price():
    engine = make_engine(retail_prices_handler())
    sp1 = await engine.get_vm_savings_plan_monthly_price("eastus", "Standard_D2s_v3", "1 Year")
    sp3 = await engine.get_vm_savings_plan_monthly_price("eastus", "Standard_D2s_v3", "3 Years")
    assert sp1 == round(0.06 * HOURS_PER_MONTH, 2)
    assert sp3 == round(0.05 * HOURS_PER_MONTH, 2)


async def test_vm_savings_plan_none_when_absent():
    engine = make_engine(retail_prices_handler())
    assert await engine.get_vm_savings_plan_monthly_price("eastus", "Standard_Nonexistent") is None


async def test_reserved_price_rejects_bad_term():
    engine = make_engine(retail_prices_handler())
    with pytest.raises(ValueError):
        await engine.get_vm_reserved_monthly_price("eastus", "Standard_D2s_v3", "2 Years")


async def test_unknown_sku_returns_none():
    engine = make_engine(retail_prices_handler())
    assert await engine.get_vm_hourly_price("eastus", "Standard_Nonexistent") is None


# ── Managed disk pricing ────────────────────────────────────────────────────────

async def test_managed_disk_tier_price_from_api():
    engine = make_engine(retail_prices_handler())
    price = await engine.get_managed_disk_monthly_price("eastus", "premium_lrs", 100)
    assert price == 19.71  # P10 LRS from API


async def test_managed_disk_static_fallback_for_ultrassd():
    # UltraSSD has no tier; falls back to per-GB static table (0.125/GB).
    engine = make_engine(retail_prices_handler())
    price = await engine.get_managed_disk_monthly_price("eastus", "ultrassd_lrs", 100)
    assert price == round(100 * 0.125, 2)


# ── Public IP pricing ────────────────────────────────────────────────────────────

async def test_public_ip_monthly_price_from_api():
    engine = make_engine(retail_prices_handler())
    price = await engine.get_public_ip_monthly_price("eastus", "Standard")
    assert price == round(0.005 * HOURS_PER_MONTH, 2)


# ── Caching ──────────────────────────────────────────────────────────────────────

async def test_second_call_is_cached_no_network():
    counter = [0]
    engine = make_engine(retail_prices_handler(request_counter=counter))
    await engine.get_vm_hourly_price("eastus", "Standard_D2s_v3")
    await engine.get_vm_hourly_price("eastus", "Standard_D2s_v3")
    assert counter[0] == 1  # second call served from cache


# ── Pagination ────────────────────────────────────────────────────────────────────

async def test_pagination_follows_next_page_link():
    counter = [0]
    engine = make_engine(retail_prices_handler(request_counter=counter, paginate_vms=True))
    price = await engine.get_vm_hourly_price("eastus", "Standard_D2s_v3")
    assert counter[0] == 2       # two pages fetched
    assert price == 0.096        # items from both pages merged


# ── Resilience / fallback ─────────────────────────────────────────────────────────

async def test_serves_last_known_good_when_api_down():
    down = {"flag": False}
    counter = [0]
    cache = InMemoryTTLCache()
    engine = make_engine(
        retail_prices_handler(request_counter=counter, fail=lambda: down["flag"]),
        ttl_seconds=0,   # entries expire immediately, forcing a re-fetch attempt
        cache=cache,
    )
    # 1) First call succeeds and populates last-known-good.
    first = await engine.get_vm_hourly_price("eastus", "Standard_D2s_v3")
    assert first == 0.096
    # 2) API goes down; TTL already expired → falls back to stale cached value.
    down["flag"] = True
    second = await engine.get_vm_hourly_price("eastus", "Standard_D2s_v3")
    assert second == 0.096


async def test_raises_when_api_down_and_no_cache():
    engine = make_engine(retail_prices_handler(fail=lambda: True))
    with pytest.raises(PricingUnavailableError):
        await engine.get_vm_hourly_price("eastus", "Standard_D2s_v3")


# ── Daily refresh ─────────────────────────────────────────────────────────────────

async def test_refresh_tracked_refetches_cached_keys():
    counter = [0]
    engine = make_engine(retail_prices_handler(request_counter=counter))
    await engine.get_vm_hourly_price("eastus", "Standard_D2s_v3")  # 1 fetch, now cached
    refreshed = await engine.refresh_tracked()
    assert refreshed == 1
    assert counter[0] == 2  # refresh re-fetched the one tracked key


# ── Flat-rate live pricing (Load Balancer / NAT / Bastion / ASP) ───────────────────

def _flat_response(retail_price, meter="Standard Included LB Rules"):
    def handler(_req):
        return httpx.Response(200, json={"Items": [{
            "retailPrice": retail_price, "type": "Consumption", "unitOfMeasure": "1 Hour",
            "meterName": meter, "productName": "Load Balancer",
        }], "NextPageLink": None})
    return handler


async def test_load_balancer_uses_live_price_when_sane():
    price = await make_engine(_flat_response(0.025)).get_load_balancer_monthly_price("eastus")
    assert price == round(0.025 * HOURS_PER_MONTH, 2)  # 18.25 — straight from the live meter


async def test_flat_price_falls_back_when_empty():
    from app.services.estimates import LOAD_BALANCER_MONTHLY_USD
    empty = lambda _req: httpx.Response(200, json={"Items": [], "NextPageLink": None})
    assert await make_engine(empty).get_load_balancer_monthly_price("eastus") == LOAD_BALANCER_MONTHLY_USD


async def test_flat_price_rejects_out_of_band_meter():
    # A wrong meter match (10x the fallback) is rejected in favour of the dated estimate.
    from app.services.estimates import LOAD_BALANCER_MONTHLY_USD
    price = await make_engine(_flat_response(5.0, meter="Rule")).get_load_balancer_monthly_price("eastus")
    assert price == LOAD_BALANCER_MONTHLY_USD
