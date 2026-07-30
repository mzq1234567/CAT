"""
Request-context middleware (Step 7).

Assigns/propagates a request id, times each request, and emits one structured access-log line per
request. The request id is echoed in the `X-Request-Id` response header for client-side correlation.
"""
from __future__ import annotations

import logging
import time
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from .logging_config import request_id_var

logger = logging.getLogger("cat.request")


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid4().hex
        token = request_id_var.set(request_id)
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-Id"] = request_id
            return response
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            logger.info(
                "request",
                extra={"extra_fields": {
                    "method": request.method,
                    "path": request.url.path,
                    "status": status,
                    "duration_ms": duration_ms,
                }},
            )
            request_id_var.reset(token)
