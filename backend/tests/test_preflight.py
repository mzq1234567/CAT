"""Tests for the self-service permission pre-flight readiness check (Phase B)."""
from __future__ import annotations

import httpx
import pytest

from app.services import preflight as pf
from app.services.azure_client import AzureClient
from app.services.pricing import PricingUnavailableError

SUB = "00000000-0000-0000-0000-000000000001"
SERVICE_COLS = [{"name": "Cost"}, {"name": "ServiceName"}, {"name": "Currency"}]


class _StubPricing:
    def __init__(self, price=70.0):
        self.price = price

    async def get_vm_monthly_price(self, region, sku):
        if self.price is None:
            raise PricingUnavailableError("down")
        return self.price


def _client(*, sub_accessible=True, arg_status=200, cost_rows=None):
    cost_rows = cost_rows if cost_rows is not None else [[100.0, "Virtual Machines", "USD"]]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/subscriptions"):
            subs = [{"subscriptionId": SUB, "state": "Enabled",
                     "displayName": "Contoso Prod", "tenantId": "tenant-1"}] if sub_accessible else []
            return httpx.Response(200, json={"value": subs})
        if "/providers/Microsoft.ResourceGraph/resources" in path:
            if arg_status != 200:
                return httpx.Response(arg_status, json={"error": {"message": "denied"}})
            return httpx.Response(200, json={"data": [{"id": "/r/1"}]})
        if "/providers/Microsoft.CostManagement/query" in path:
            return httpx.Response(200, json={"properties": {"columns": SERVICE_COLS, "rows": cost_rows}})
        return httpx.Response(200, json={})

    return AzureClient("t", transport=httpx.MockTransport(handler), base_delay=0.001)


def _status(report, key):
    return next(c["status"] for c in report["checks"] if c["key"] == key)


@pytest.fixture(autouse=True)
def _stub_pricing(monkeypatch):
    monkeypatch.setattr(pf, "get_pricing_engine", lambda currency=None: _StubPricing())


async def test_preflight_all_ready():
    report = await pf.run_preflight(_client(), SUB, user_email="u@x.com")
    assert report["ready"] is True
    assert report["subscription_name"] == "Contoso Prod"
    assert report["tenant_id"] == "tenant-1"
    assert all(_status(report, k) == "ok" for k in ("signin", "subscription", "inventory", "cost", "metrics", "pricing"))


async def test_preflight_cost_unavailable_is_warning_not_blocking():
    # No billed cost returned → cost is a WARNING, but the assessment can still run (findings degrade
    # to "not quantified", never fabricated).
    report = await pf.run_preflight(_client(cost_rows=[]), SUB, user_email="u@x.com")
    assert report["ready"] is True                      # cost is non-blocking
    assert _status(report, "cost") == "warning"
    cost = next(c for c in report["checks"] if c["key"] == "cost")
    assert "not be quantified" in cost["detail"]
    # No Cost Management Reader role is demanded, and no raw HTTP status leaks.
    assert "Cost Management Reader" not in cost["detail"] and "403" not in cost["detail"]


async def test_preflight_no_subscription_access_is_blocking():
    report = await pf.run_preflight(_client(sub_accessible=False), SUB, user_email="u@x.com")
    assert report["ready"] is False
    sub = next(c for c in report["checks"] if c["key"] == "subscription")
    assert sub["status"] == "unavailable" and sub["blocking"] is True
    assert "403" not in sub["detail"]                   # client-safe wording only


async def test_preflight_inventory_failure_is_blocking():
    report = await pf.run_preflight(_client(arg_status=500), SUB, user_email="u@x.com")
    assert report["ready"] is False
    inv = next(c for c in report["checks"] if c["key"] == "inventory")
    assert inv["status"] == "unavailable" and inv["blocking"] is True


async def test_preflight_pricing_unavailable_is_warning(monkeypatch):
    monkeypatch.setattr(pf, "get_pricing_engine", lambda currency=None: _StubPricing(price=None))
    report = await pf.run_preflight(_client(), SUB, user_email="u@x.com")
    assert report["ready"] is True                      # pricing is non-blocking
    assert _status(report, "pricing") == "warning"


# ── Route: auth + input validation ───────────────────────────────────────────────────

def test_preflight_route_rejects_bad_guid(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.dependencies import get_current_user
    from app.api.routes import assessments as routes

    app = FastAPI()
    app.include_router(routes.router, prefix="/api/assessments")
    app.dependency_overrides[get_current_user] = lambda: {"token": "t", "email": "u@x.com",
                                                          "user_id": "u", "tenant_id": "t1"}
    client = TestClient(app)
    assert client.get("/api/assessments/preflight?subscription_id=not-a-guid").status_code == 400


def test_preflight_route_returns_report(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.dependencies import get_current_user
    from app.api.routes import assessments as routes

    async def _stub(client, subscription_id, user_email=None):
        return {"subscription_id": subscription_id, "subscription_name": "Contoso", "tenant_id": "t1",
                "ready": True, "checks": [{"key": "signin", "label": "Microsoft sign-in",
                                           "status": "ok", "detail": "", "blocking": False}]}
    monkeypatch.setattr(routes, "run_preflight", _stub)

    app = FastAPI()
    app.include_router(routes.router, prefix="/api/assessments")
    app.dependency_overrides[get_current_user] = lambda: {"token": "t", "email": "u@x.com",
                                                          "user_id": "u", "tenant_id": "t1"}
    client = TestClient(app)
    r = client.get(f"/api/assessments/preflight?subscription_id={SUB}")
    assert r.status_code == 200 and r.json()["ready"] is True
