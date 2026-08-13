"""
Azure AD access-token signature verification (security hardening).

Background: this tool uses delegated auth — the caller's ARM access token. Endpoints that call
Azure are implicitly protected (Azure rejects a bad token). But the endpoints that only READ stored
assessment data authorize purely on the token's `oid`/`tid` claims and never touch Azure — so if the
token isn't cryptographically verified, anyone can forge one with a victim's `oid`+`tid` and read or
download their data. That is a tenant-isolation bypass.

This module verifies the RS256 signature against Microsoft's published JWKS (Azure AD public keys),
plus expiry (and audience, optionally). It pins RS256 with an RSA public key, which also blocks the
classic `alg=none` / HS256-confusion attacks.

JWKS is fetched from the Azure AD "common" OIDC metadata and cached; on an unknown `kid` (key
rotation) the cache is force-refreshed once before failing. Network is injectable for tests.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Dict, List, Optional

import httpx
import jwt

logger = logging.getLogger("cat.auth")

OIDC_CONFIG_URL = "https://login.microsoftonline.com/common/.well-known/openid-configuration"


class TokenVerificationError(Exception):
    """Raised when a token cannot be trusted (bad signature, expired, malformed, unknown key)."""


# Legitimate Microsoft token issuer prefixes (v1 `sts.windows.net`, v2 `login.microsoftonline.com`).
# A valid Entra token's `iss` starts with one of these followed by the issuing tenant's GUID.
_MS_ISSUER_PREFIXES = (
    "https://sts.windows.net/",
    "https://login.microsoftonline.com/",
    "https://login.microsoft.com/",
    "https://login.windows.net/",
)


class TokenVerifier:
    def __init__(
        self,
        allowed_audiences: Optional[List[str]] = None,
        enforce_audience: bool = False,
        jwks_ttl_seconds: int = 3600,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        timeout: float = 10.0,
        require_issuer: bool = False,
        expected_appids: Optional[List[str]] = None,
        require_delegated: bool = False,
    ):
        self._audiences = allowed_audiences or []
        self._enforce_audience = enforce_audience
        self._jwks_ttl = jwks_ttl_seconds
        self._transport = transport
        self._timeout = timeout
        # Additional claim checks beyond signature/expiry/audience. Together these prove the token was
        # issued by Microsoft, for THIS application, for a specific tenant, and represents a signed-in user.
        self._require_issuer = require_issuer
        self._expected_appids = [a for a in (expected_appids or []) if a]  # our client id(s)
        self._require_delegated = require_delegated
        self._keys: Dict[str, object] = {}   # kid -> RSA public key
        self._jwks_uri: Optional[str] = None
        self._fetched_at = 0.0

    async def _fetch_keys(self) -> None:
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            if self._jwks_uri is None:
                meta = await client.get(OIDC_CONFIG_URL)
                meta.raise_for_status()
                self._jwks_uri = meta.json()["jwks_uri"]
            jwks_resp = await client.get(self._jwks_uri)
            jwks_resp.raise_for_status()
            jwks = jwks_resp.json()

        keys: Dict[str, object] = {}
        for jwk in jwks.get("keys", []):
            kid = jwk.get("kid")
            if not kid:
                continue
            try:
                keys[kid] = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
            except Exception:  # skip malformed / non-RSA keys, keep the rest
                continue
        self._keys = keys
        self._fetched_at = time.monotonic()

    async def _ensure_keys(self, force: bool = False) -> None:
        fresh = self._keys and (time.monotonic() - self._fetched_at) < self._jwks_ttl
        if force or not fresh:
            await self._fetch_keys()

    async def verify(self, token: str) -> dict:
        """Return the token's claims iff its RS256 signature (and expiry) are valid. Else raise."""
        try:
            header = jwt.get_unverified_header(token)
        except Exception as exc:
            raise TokenVerificationError("malformed token header") from exc

        if header.get("alg") != "RS256":
            # Reject alg=none / HS256 confusion outright.
            raise TokenVerificationError(f"unexpected token algorithm: {header.get('alg')!r}")
        kid = header.get("kid")
        if not kid:
            raise TokenVerificationError("token has no key id")

        await self._ensure_keys()
        key = self._keys.get(kid)
        if key is None:
            await self._ensure_keys(force=True)  # possible key rotation → refresh once
            key = self._keys.get(kid)
        if key is None:
            raise TokenVerificationError("signing key not found in JWKS")

        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                audience=self._audiences or None,
                options={"verify_aud": self._enforce_audience},
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenVerificationError("token expired") from exc
        except jwt.InvalidTokenError as exc:
            raise TokenVerificationError("invalid token signature or claims") from exc

        # Signature, expiry and (optionally) audience are now verified. Enforce the remaining claims that
        # prove *provenance* — that this token really is one Microsoft issued for our app + this user.
        self._verify_claims(claims)
        return claims

    def _verify_claims(self, claims: dict) -> None:
        tid = claims.get("tid")
        if not tid:
            raise TokenVerificationError("token has no tenant id (tid)")

        if self._require_issuer:
            iss = claims.get("iss") or ""
            if not any(iss.startswith(p) for p in _MS_ISSUER_PREFIXES):
                raise TokenVerificationError("token issuer is not a recognised Microsoft issuer")
            # The issuer must belong to the SAME tenant the token claims (`tid`), so a token can't claim
            # one tenant while being issued by another.
            if tid not in iss:
                raise TokenVerificationError("token issuer does not match its tenant id")

        if self._expected_appids:
            # `appid` (v1) / `azp` (v2) identifies the client application the token was issued to. It must
            # be OUR registration — a token minted for a different app (even same user + ARM audience) is
            # rejected. This is the "issued for our application" guarantee.
            appid = claims.get("appid") or claims.get("azp")
            if appid not in self._expected_appids:
                raise TokenVerificationError("token was not issued for this application")

        if self._require_delegated:
            # A delegated (user) token carries `scp` (scopes). An app-only token carries `roles` and no
            # `scp`. We use delegated auth only, so reject app-only tokens — they don't represent a user.
            if not claims.get("scp") and claims.get("roles"):
                raise TokenVerificationError("app-only token rejected; a delegated user token is required")
