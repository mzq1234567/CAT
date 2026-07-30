import logging
from typing import Optional

import jwt
from fastapi import Header, HTTPException

from ..config import settings

logger = logging.getLogger("cat.auth")

# Lazily-created shared verifier (holds the cached JWKS).
_verifier = None


def get_verifier():
    from ..security.token import TokenVerifier
    global _verifier
    if _verifier is None:
        _verifier = TokenVerifier(
            allowed_audiences=settings.token_allowed_audiences,
            enforce_audience=settings.token_enforce_audience,
        )
    return _verifier


def _claims_to_user(token: str, decoded: dict) -> dict:
    user_id = decoded.get("oid") or decoded.get("sub", "unknown")
    tenant_id = decoded.get("tid", "unknown")  # tenant isolation boundary (Step 7)
    email = (
        decoded.get("upn")
        or decoded.get("preferred_username")
        or decoded.get("email")
        or "unknown"
    )
    return {"token": token, "user_id": user_id, "tenant_id": tenant_id, "email": email}


async def get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = authorization.split(" ", 1)[1]

    if settings.verify_token_signature:
        from ..security.token import TokenVerificationError
        try:
            decoded = await get_verifier().verify(token)
        except TokenVerificationError:
            # Do not echo the underlying reason to the caller (avoid oracle / info leak).
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        except Exception:
            logger.exception("Token verification failed unexpectedly")
            raise HTTPException(status_code=401, detail="Token could not be verified")
        return _claims_to_user(token, decoded)

    # Signature verification disabled (dev only). We still parse claims for identity.
    try:
        decoded = jwt.decode(token, options={"verify_signature": False}, algorithms=["RS256"])
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")
    return _claims_to_user(token, decoded)
