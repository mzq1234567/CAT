"""Helpers to mint real RS256 tokens + a mock Azure AD JWKS endpoint for auth tests."""
from __future__ import annotations

import json
import time
from typing import Optional

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

JWKS_URI = "https://login.microsoftonline.com/common/discovery/v2.0/keys"


def make_keypair(kid: str = "test-kid"):
    """Return (private_key_pem, public_jwk_dict) for an RSA-2048 signing key."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return priv_pem, jwk


def make_token(priv_pem, kid: str = "test-kid", **claims) -> str:
    now = int(time.time())
    payload = {
        "oid": "user-1", "tid": "tenant-1", "upn": "u@x.com",
        "aud": "https://management.azure.com/", "iat": now, "exp": now + 3600,
    }
    payload.update(claims)
    return jwt.encode(payload, priv_pem, algorithm="RS256", headers={"kid": kid})


def jwks_transport(jwk: dict, request_counter: Optional[list] = None) -> httpx.MockTransport:
    """MockTransport serving the OIDC discovery doc + a JWKS containing `jwk`."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request_counter is not None:
            request_counter[0] += 1
        if "openid-configuration" in request.url.path:
            return httpx.Response(200, json={"jwks_uri": JWKS_URI})
        return httpx.Response(200, json={"keys": [jwk]})

    return httpx.MockTransport(handler)
