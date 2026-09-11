"""Device-code sign-in endpoints.

These are the ONLY endpoints that are not behind `get_current_user` — they are the login itself.
The browser calls `/start` to begin, shows the returned code, then polls `/poll` until the sign-in
completes and it receives the delegated ARM access token (which it then sends as Bearer on every other
call). See security/device_code.py for why the flow is backend-driven.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...security.device_code import DeviceCodeError, get_authenticator

router = APIRouter()


class DeviceStartResponse(BaseModel):
    session_id: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


class DevicePollRequest(BaseModel):
    session_id: str


class DeviceAccount(BaseModel):
    user_id: str
    tenant_id: str
    email: str


class DevicePollResponse(BaseModel):
    status: str  # "pending" | "complete"
    access_token: str | None = None
    expires_on: int | None = None
    account: DeviceAccount | None = None


@router.post("/device/start", response_model=DeviceStartResponse)
async def device_start():
    try:
        result = await get_authenticator().start()
    except DeviceCodeError as exc:
        raise HTTPException(status_code=400, detail=exc.message)
    return DeviceStartResponse(**result)


@router.post("/device/poll", response_model=DevicePollResponse)
async def device_poll(body: DevicePollRequest):
    try:
        result = await get_authenticator().poll(body.session_id)
    except DeviceCodeError as exc:
        # 401 for expired/declined (start over), 400 for other terminal errors.
        status = 401 if exc.code in ("expired", "declined") else 400
        raise HTTPException(status_code=status, detail=exc.message)
    return DevicePollResponse(**result)
