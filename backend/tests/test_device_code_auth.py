"""Device-code sign-in tests — drive the backend flow against a mock Entra (no network).

These cover the authenticator (start/poll state machine + Entra error mapping) and the two routes.
They do NOT reach Microsoft; a MockTransport plays the /devicecode and /token endpoints.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.security import device_code as dc
from app.security.device_code import DeviceCodeAuthenticator, DeviceCodeError
from app.api.routes import auth as auth_routes
from tests.jwt_helpers import make_keypair, make_token

AUTHORITY = "https://login.microsoftonline.com/organizations"
SCOPE = "https://management.azure.com/user_impersonation openid profile"


def _mock_transport(token_responses):
    """token_responses: list of dicts -> httpx.Response bodies returned by successive /token POSTs.
    Each entry is (status_code, json_body). The /devicecode POST always returns a ready session with
    interval=0 so polls aren't time-gated in tests."""
    calls = iter(token_responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/devicecode"):
            return httpx.Response(200, json={
                "device_code": "SECRET-DEVICE-CODE",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://microsoft.com/devicelogin",
                "expires_in": 900,
                "interval": 0,
            })
        if request.url.path.endswith("/token"):
            status, body = next(calls)
            return httpx.Response(status, json=body)
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler)


def _authenticator(token_responses):
    return DeviceCodeAuthenticator(
        client_id="04b07795-8ddb-461a-bbee-02f9e1bf7b46",
        authority=AUTHORITY,
        scope=SCOPE,
        transport=_mock_transport(token_responses),
    )


async def test_start_returns_user_code_and_hides_device_code():
    auth = _authenticator([])
    started = await auth.start()
    assert started["user_code"] == "ABCD-EFGH"
    assert started["verification_uri"] == "https://microsoft.com/devicelogin"
    assert "session_id" in started and started["session_id"]
    # The polling secret must never be exposed to the caller/browser.
    assert "device_code" not in started


async def test_poll_pending_then_complete_returns_token_and_account():
    priv, _ = make_keypair()
    access = make_token(priv, oid="alice", tid="tenant-A", upn="alice@contoso.com")
    auth = _authenticator([
        (400, {"error": "authorization_pending"}),
        (200, {"access_token": access, "expires_in": 3599}),
    ])
    started = await auth.start()
    sid = started["session_id"]

    assert (await auth.poll(sid))["status"] == "pending"
    done = await auth.poll(sid)
    assert done["status"] == "complete"
    assert done["access_token"] == access
    assert done["account"] == {"user_id": "alice", "tenant_id": "tenant-A", "email": "alice@contoso.com"}
    assert done["expires_on"] > 0


async def test_poll_expired_token_raises_expired():
    auth = _authenticator([(400, {"error": "expired_token"})])
    sid = (await auth.start())["session_id"]
    with pytest.raises(DeviceCodeError) as exc:
        await auth.poll(sid)
    assert exc.value.code == "expired"


async def test_poll_declined_raises_declined():
    auth = _authenticator([(400, {"error": "authorization_declined"})])
    sid = (await auth.start())["session_id"]
    with pytest.raises(DeviceCodeError) as exc:
        await auth.poll(sid)
    assert exc.value.code == "declined"
    assert "cancelled" in exc.value.message.lower() or "denied" in exc.value.message.lower()


async def test_poll_conditional_access_maps_to_policy_message():
    auth = _authenticator([(400, {
        "error": "access_denied",
        "error_description": "AADSTS53003: Access has been blocked by Conditional Access policies.",
    })])
    sid = (await auth.start())["session_id"]
    with pytest.raises(DeviceCodeError) as exc:
        await auth.poll(sid)
    assert "security policy" in exc.value.message.lower()
    # Never leak the raw AADSTS description to the client.
    assert "AADSTS" not in exc.value.message


async def test_poll_slow_down_stays_pending_and_backs_off():
    auth = _authenticator([(400, {"error": "slow_down"})])
    started = await auth.start()
    sid = started["session_id"]
    assert (await auth.poll(sid))["status"] == "pending"
    # After slow_down the next poll is time-gated, so an immediate re-poll returns pending without a call.
    assert (await auth.poll(sid))["status"] == "pending"


async def test_poll_unknown_session_is_expired():
    auth = _authenticator([])
    with pytest.raises(DeviceCodeError) as exc:
        await auth.poll("no-such-session")
    assert exc.value.code == "expired"


async def test_start_invalid_client_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_client",
                                         "error_description": "AADSTS7000218"})
    auth = DeviceCodeAuthenticator(
        client_id="x", authority=AUTHORITY, scope=SCOPE,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(DeviceCodeError) as exc:
        await auth.start()
    assert "administrator" in exc.value.message.lower()
    assert "AADSTS" not in exc.value.message


# ── Route-level ─────────────────────────────────────────────────────────────────────

def _client_with_mock(monkeypatch, token_responses):
    app = FastAPI()
    app.include_router(auth_routes.router, prefix="/api/auth")
    monkeypatch.setattr(dc, "_authenticator", _authenticator(token_responses), raising=False)
    return TestClient(app)


def test_route_start_then_poll_completes(monkeypatch):
    priv, _ = make_keypair()
    access = make_token(priv, oid="bob", tid="tenant-B", upn="bob@contoso.com")
    client = _client_with_mock(monkeypatch, [
        (400, {"error": "authorization_pending"}),
        (200, {"access_token": access, "expires_in": 3599}),
    ])
    started = client.post("/api/auth/device/start").json()
    assert started["user_code"] == "ABCD-EFGH"
    sid = started["session_id"]

    assert client.post("/api/auth/device/poll", json={"session_id": sid}).json()["status"] == "pending"
    done = client.post("/api/auth/device/poll", json={"session_id": sid}).json()
    assert done["status"] == "complete"
    assert done["access_token"] == access
    assert done["account"]["email"] == "bob@contoso.com"


def test_route_poll_expired_returns_401(monkeypatch):
    client = _client_with_mock(monkeypatch, [(400, {"error": "expired_token"})])
    sid = client.post("/api/auth/device/start").json()["session_id"]
    resp = client.post("/api/auth/device/poll", json={"session_id": sid})
    assert resp.status_code == 401
