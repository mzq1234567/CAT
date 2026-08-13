"""
Currency correctness — billing currency must equal pricing currency must equal displayed currency, and
USD must NEVER be silently substituted when the billing currency is known or unsupported.
"""
from __future__ import annotations

import httpx
import pytest

from app.services.cost_management import extract_currency, get_cost_map_and_consistency
from app.services.azure_client import AzureClient
from app.services.pricing import (
    PricingEngine,
    RETAIL_SUPPORTED_CURRENCIES,
    get_pricing_engine,
    pricing_currency_supported,
)

CURRENCIES = ["USD", "INR", "AUD", "EUR"]


def _vm_item(currency: str, price: float = 0.10) -> dict:
    return {
        "currencyCode": currency, "retailPrice": price, "unitPrice": price, "type": "Consumption",
        "meterName": "D2s v3", "productName": "Virtual Machines D2s v3", "unitOfMeasure": "1 Hour",
        "armSkuName": "Standard_D2s_v3", "armRegionName": "eastus",
    }


def _honoring_transport() -> httpx.MockTransport:
    """A Retail Prices API that HONOURS the requested currencyCode (returns prices in it)."""
    def handler(request: httpx.Request) -> httpx.Response:
        cur = request.url.params.get("currencyCode", "USD")
        return httpx.Response(200, json={"Items": [_vm_item(cur)], "NextPageLink": None})
    return httpx.MockTransport(handler)


def _usd_only_transport() -> httpx.MockTransport:
    """A Retail Prices API that IGNORES currencyCode and always answers in USD (the unsupported case)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Items": [_vm_item("USD")], "NextPageLink": None})
    return httpx.MockTransport(handler)


# ── Pricing is requested AND returned in the billing currency ────────────────────────────────────

@pytest.mark.parametrize("currency", CURRENCIES)
async def test_pricing_uses_billing_currency(currency):
    engine = PricingEngine(currency=currency, transport=_honoring_transport())
    price = await engine.get_vm_monthly_price("eastus", "Standard_D2s_v3")
    assert price == round(0.10 * 730, 2)      # a real price, in the requested currency (0.10/hr → monthly)


@pytest.mark.parametrize("currency", CURRENCIES)
def test_get_pricing_engine_currency_matches_billing(currency):
    # billing currency = pricing engine currency (never silently USD for a known currency).
    engine = get_pricing_engine(currency)
    assert engine._currency.upper() == currency


# ── The mislabel guard: a USD answer to a non-USD request yields NO price (never a mislabelled USD) ──

async def test_usd_answer_to_inr_request_is_dropped_not_mislabelled():
    engine = PricingEngine(currency="INR", transport=_usd_only_transport())
    price = await engine.get_vm_monthly_price("eastus", "Standard_D2s_v3")
    assert price is None      # the finding degrades to REVIEW rather than showing a USD figure as ₹


async def test_usd_request_keeps_usd_prices():
    engine = PricingEngine(currency="USD", transport=_usd_only_transport())
    price = await engine.get_vm_monthly_price("eastus", "Standard_D2s_v3")
    assert price == round(0.10 * 730, 2)      # USD asked, USD returned → kept


# ── Supported-currency helper ─────────────────────────────────────────────────────────────────────

def test_supported_currencies():
    for c in CURRENCIES:
        assert pricing_currency_supported(c) is True
    assert "USD" in RETAIL_SUPPORTED_CURRENCIES and "INR" in RETAIL_SUPPORTED_CURRENCIES
    assert pricing_currency_supported("ZZZ") is False
    assert pricing_currency_supported(None) is False


# ── Currency is read from the real Cost Management response ────────────────────────────────────────

def test_extract_currency_reads_the_column():
    payload = {"properties": {
        "columns": [{"name": "Cost"}, {"name": "ResourceId"}, {"name": "Currency"}],
        "rows": [[10.0, "/r/1", "INR"]]}}
    assert extract_currency(payload) == "INR"
    assert extract_currency({"properties": {"columns": [{"name": "Cost"}], "rows": [[10.0]]}}) is None


async def test_cost_map_detects_currency_from_billing_response():
    rid = "/subscriptions/s/rg/x"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"properties": {
            "columns": [{"name": "Cost"}, {"name": "ResourceId"}, {"name": "BillingMonth"}, {"name": "Currency"}],
            "rows": [[100.0, rid, "2025-05-01", "INR"], [110.0, rid, "2025-06-01", "INR"]]}})

    client = AzureClient("t", transport=httpx.MockTransport(handler))
    cost_map, cons, totals, basis, currency = await get_cost_map_and_consistency(client, "sub-1")
    assert currency == "INR"      # per-resource billing is a valid currency source (not just service costs)
