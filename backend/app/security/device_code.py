"""
OAuth 2.0 device authorization grant (device-code) sign-in, driven by the backend.

Why backend-driven: the Microsoft `/devicecode` and `/token` endpoints do not send CORS headers for
browser origins, so a single-page app cannot run the polling loop itself. The backend therefore acts as
the OAuth *public client* — it starts the flow, holds the short-lived `device_code` in memory keyed by an
opaque session id, and polls `/token` on the frontend's behalf. On success it hands the resulting
delegated Azure Resource Manager access token back to the browser, which continues to send it as
`Authorization: Bearer` on every API call. Everything downstream (token verification in
`get_current_user`, `AzureClient`, RBAC, the assessment engine) is unchanged.

Security notes:
- The `device_code` (a polling credential) is NEVER returned to the browser; only the opaque session id,
  the human `user_code`, and the verification URI are. Access tokens are never logged.
- This module does not persist anything: the session store is in-memory and self-expiring.
- The returned access token is fully re-verified (signature/issuer/audience/appid/delegated) on every
  subsequent authenticated request by `get_current_user` — this module does not weaken that gate.
"""
from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from typing import Dict, Optional

import httpx
import jwt

logger = logging.getLogger("cat.auth")

DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class DeviceCodeError(Exception):
    """A device-code sign-in could not be started or completed (safe, client-facing message)."""

    def __init__(self, message: str, *, code: str = "error"):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class _Session:
    device_code: str
    interval: int
    expires_at: float
    next_poll_at: float


class DeviceCodeAuthenticator:
    """Starts and polls device-code sign-ins. One instance is shared per process (see get_authenticator).

    `transport` is injectable so tests can drive the flow against a mock Entra without network.
    """

    def __init__(
        self,
        client_id: str,
        authority: str,
        scope: str,
        session_ttl_seconds: int = 900,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        timeout: float = 15.0,
    ):
        self._client_id = client_id
        self._authority = authority.rstrip("/")
        self._scope = scope
        self._ttl = session_ttl_seconds
        self._transport = transport
        self._timeout = timeout
        self._sessions: Dict[str, _Session] = {}

    # ── public API ────────────────────────────────────────────────────────────────────
    async def start(self) -> dict:
        """Begin a device-code sign-in. Returns the fields the UI shows the user, plus an opaque
        `session_id` used to poll. The `device_code` is kept server-side and never exposed."""
        self._evict_expired()
        url = f"{self._authority}/oauth2/v2.0/devicecode"
        data = {"client_id": self._client_id, "scope": self._scope}
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                resp = await client.post(url, data=data)
        except httpx.HTTPError as exc:
            logger.warning("device-code start: network error contacting Entra: %s", type(exc).__name__)
            raise DeviceCodeError("Could not reach Microsoft sign-in. Please try again.") from exc

        body = _safe_json(resp)
        if resp.status_code != 200 or "device_code" not in body:
            raise DeviceCodeError(_map_start_error(body), code=body.get("error", "error"))

        expires_in = int(body.get("expires_in", 900))
        ttl = min(expires_in, self._ttl)
        interval = int(body.get("interval", 5))
        session_id = secrets.token_urlsafe(24)
        now = time.monotonic()
        self._sessions[session_id] = _Session(
            device_code=body["device_code"],
            interval=interval,
            expires_at=now + ttl,
            next_poll_at=now,
        )
        # `device_code`, `message` (may embed the code) are intentionally NOT forwarded verbatim.
        return {
            "session_id": session_id,
            "user_code": body["user_code"],
            "verification_uri": body.get("verification_uri", "https://microsoft.com/devicelogin"),
            "expires_in": ttl,
            "interval": interval,
        }

    async def poll(self, session_id: str) -> dict:
        """Poll a pending sign-in. Returns one of:
          {"status": "pending"}
          {"status": "complete", "access_token", "expires_on", "account": {...}}
        or raises DeviceCodeError (expired / declined / conditional-access / other), which the route
        maps to a clean client message."""
        self._evict_expired()
        session = self._sessions.get(session_id)
        if session is None:
            raise DeviceCodeError("Your sign-in code expired. Start a new sign-in attempt.", code="expired")

        now = time.monotonic()
        if now >= session.expires_at:
            self._sessions.pop(session_id, None)
            raise DeviceCodeError("Your sign-in code expired. Start a new sign-in attempt.", code="expired")
        if now < session.next_poll_at:
            return {"status": "pending"}

        url = f"{self._authority}/oauth2/v2.0/token"
        data = {
            "grant_type": DEVICE_CODE_GRANT,
            "client_id": self._client_id,
            "device_code": session.device_code,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                resp = await client.post(url, data=data)
        except httpx.HTTPError as exc:
            logger.warning("device-code poll: network error contacting Entra: %s", type(exc).__name__)
            # Transient: let the client keep polling rather than failing the whole sign-in.
            return {"status": "pending"}

        body = _safe_json(resp)
        if resp.status_code == 200 and "access_token" in body:
            self._sessions.pop(session_id, None)
            return self._complete(body)

        error = body.get("error", "")
        if error in ("authorization_pending", "slow_down"):
            if error == "slow_down":
                session.interval += 5
            session.next_poll_at = time.monotonic() + session.interval
            return {"status": "pending"}

        # Terminal errors — drop the session and surface a safe, mapped message.
        self._sessions.pop(session_id, None)
        raise DeviceCodeError(_map_poll_error(error, body), code=_poll_error_code(error))

    # ── internals ───────────────────────────────────────────────────────────────────
    def _complete(self, body: dict) -> dict:
        access_token = body["access_token"]
        # Decode WITHOUT verifying — display identity only. The token is fully verified on every
        # subsequent authenticated request by get_current_user; this must not become a second, weaker
        # verification path, so we deliberately do not trust these claims for authorization.
        try:
            claims = jwt.decode(access_token, options={"verify_signature": False}, algorithms=["RS256"])
        except Exception:
            claims = {}
        account = {
            "user_id": claims.get("oid") or claims.get("sub", "unknown"),
            "tenant_id": claims.get("tid", "unknown"),
            "email": (
                claims.get("upn")
                or claims.get("preferred_username")
                or claims.get("email")
                or "unknown"
            ),
        }
        expires_on = int(time.time()) + int(body.get("expires_in", 3599))
        return {
            "status": "complete",
            "access_token": access_token,
            "expires_on": expires_on,
            "account": account,
        }

    def _evict_expired(self) -> None:
        now = time.monotonic()
        stale = [sid for sid, s in self._sessions.items() if now >= s.expires_at]
        for sid in stale:
            self._sessions.pop(sid, None)


def _safe_json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _map_start_error(body: dict) -> str:
    error = body.get("error", "")
    if error in ("invalid_client", "unauthorized_client"):
        return (
            "Microsoft did not allow this sign-in method for your organization. "
            "Please contact your Azure/Entra administrator."
        )
    return "Could not start Microsoft sign-in. Please try again."


def _poll_error_code(error: str) -> str:
    if error == "expired_token":
        return "expired"
    if error == "authorization_declined":
        return "declined"
    return "error"


def _map_poll_error(error: str, body: dict) -> str:
    if error == "expired_token":
        return "Your sign-in code expired. Start a new sign-in attempt."
    if error == "authorization_declined":
        return "Sign-in was cancelled or denied."
    if error == "bad_verification_code":
        return "The sign-in code was not recognised. Start a new sign-in attempt."
    # Conditional Access and other tenant-policy blocks arrive here (e.g. AADSTS50005, 53003, 65004).
    desc = body.get("error_description") or ""
    if "AADSTS" in desc and ("conditional access" in desc.lower() or "53003" in desc or "50005" in desc):
        return (
            "Your organization's security policy blocked this sign-in method. Please contact your "
            "Azure/Entra administrator or use an approved authentication method."
        )
    if error == "access_denied":
        return "Sign-in was cancelled or denied."
    return "Microsoft sign-in could not be completed. Please try again or contact your administrator."


# Shared per-process authenticator (holds the in-memory session store).
_authenticator: Optional[DeviceCodeAuthenticator] = None


def get_authenticator() -> DeviceCodeAuthenticator:
    global _authenticator
    if _authenticator is None:
        from ..config import settings

        _authenticator = DeviceCodeAuthenticator(
            client_id=settings.azure_device_code_client_id,
            authority=settings.azure_auth_authority,
            scope=settings.azure_arm_scope,
            session_ttl_seconds=settings.device_code_session_ttl_seconds,
        )
    return _authenticator
