"""Unit tests for JWKS-based token signature verification (the tenant-isolation fix)."""
from __future__ import annotations

import time

import jwt
import pytest

from app.security.token import TokenVerificationError, TokenVerifier
from tests.jwt_helpers import make_keypair, make_token, jwks_transport


def _verifier(jwk, request_counter=None, **kw):
    return TokenVerifier(transport=jwks_transport(jwk, request_counter), **kw)


async def test_valid_token_verifies_and_returns_claims():
    priv, jwk = make_keypair()
    verifier = _verifier(jwk)
    claims = await verifier.verify(make_token(priv, oid="alice", tid="tenant-A"))
    assert claims["oid"] == "alice"
    assert claims["tid"] == "tenant-A"


async def test_token_signed_by_different_key_is_rejected():
    priv_attacker, _ = make_keypair(kid="test-kid")   # attacker's private key...
    _, jwk_real = make_keypair(kid="test-kid")          # ...but JWKS holds the REAL public key
    verifier = _verifier(jwk_real)
    with pytest.raises(TokenVerificationError):
        await verifier.verify(make_token(priv_attacker))


async def test_expired_token_is_rejected():
    priv, jwk = make_keypair()
    verifier = _verifier(jwk)
    expired = make_token(priv, exp=int(time.time()) - 10)
    with pytest.raises(TokenVerificationError):
        await verifier.verify(expired)


async def test_alg_none_is_rejected():
    priv, jwk = make_keypair()
    verifier = _verifier(jwk)
    # An unsigned token claiming alg=none must never be accepted.
    forged = jwt.encode({"oid": "x"}, key="", algorithm="none", headers={"kid": "test-kid"})
    with pytest.raises(TokenVerificationError):
        await verifier.verify(forged)


async def test_hs256_confusion_is_rejected():
    priv, jwk = make_keypair()
    verifier = _verifier(jwk)
    # Classic attack: sign HS256 using the public key material as the shared secret.
    forged = jwt.encode({"oid": "x"}, key="public-key-as-secret", algorithm="HS256",
                        headers={"kid": "test-kid"})
    with pytest.raises(TokenVerificationError):
        await verifier.verify(forged)


async def test_malformed_token_is_rejected():
    priv, jwk = make_keypair()
    verifier = _verifier(jwk)
    with pytest.raises(TokenVerificationError):
        await verifier.verify("not.a.jwt")


async def test_unknown_kid_refreshes_then_fails_cleanly():
    priv, jwk = make_keypair(kid="real-kid")
    verifier = _verifier(jwk)
    token = make_token(priv, kid="rotated-kid")  # kid not present in JWKS
    with pytest.raises(TokenVerificationError):
        await verifier.verify(token)


async def test_jwks_is_cached_across_calls():
    priv, jwk = make_keypair()
    counter = [0]
    verifier = _verifier(jwk, request_counter=counter)
    await verifier.verify(make_token(priv))
    first = counter[0]
    await verifier.verify(make_token(priv))
    assert counter[0] == first  # second verify used cached keys, no new network


async def test_audience_enforced_when_configured():
    priv, jwk = make_keypair()
    verifier = _verifier(jwk, enforce_audience=True,
                         allowed_audiences=["https://management.azure.com/"])
    ok = await verifier.verify(make_token(priv, aud="https://management.azure.com/"))
    assert ok["oid"] == "user-1"
    with pytest.raises(TokenVerificationError):
        await verifier.verify(make_token(priv, aud="https://graph.microsoft.com/"))
