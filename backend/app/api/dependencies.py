from fastapi import Header, HTTPException
from typing import Optional
import jwt


async def get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = authorization.split(" ", 1)[1]

    try:
        # Decode without signature verification — we use this token directly against Azure APIs,
        # so Azure itself validates it. We only need the claims for user identity.
        decoded = jwt.decode(token, options={"verify_signature": False}, algorithms=["RS256"])
        user_id = decoded.get("oid") or decoded.get("sub", "unknown")
        email = (
            decoded.get("upn")
            or decoded.get("preferred_username")
            or decoded.get("email")
            or "unknown"
        )
        return {"token": token, "user_id": user_id, "email": email}
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")
