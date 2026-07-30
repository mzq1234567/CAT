"""Tests for RBAC subscription-access enforcement (Step 7)."""
from __future__ import annotations

import httpx
import pytest
from fastapi import HTTPException

from app.security.rbac import get_accessible_subscription_ids, verify_subscription_access
from app.services.azure_client import AzureClient


def _subscriptions_handler(sub_ids):
    def handler(request: httpx.Request) -> httpx.Response:
        value = [{"subscriptionId": s, "state": "Enabled"} for s in sub_ids]
        return httpx.Response(200, json={"value": value})
    return handler


def _client(sub_ids):
    return AzureClient("t", transport=httpx.MockTransport(_subscriptions_handler(sub_ids)))


async def test_accessible_ids():
    ids = await get_accessible_subscription_ids(_client(["sub-1", "sub-2"]))
    assert ids == {"sub-1", "sub-2"}


async def test_verify_passes_when_accessible():
    await verify_subscription_access(_client(["sub-1", "sub-2"]), ["sub-1"])  # no raise


async def test_verify_rejects_inaccessible():
    with pytest.raises(HTTPException) as exc:
        await verify_subscription_access(_client(["sub-1"]), ["sub-1", "sub-999"])
    assert exc.value.status_code == 403
    assert "sub-999" in exc.value.detail
