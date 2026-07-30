"""
Global exception handlers (Step 8; extracted for testability).

Full error detail (with stack trace + request id) is logged; the client only ever receives a
generic, stack-trace-free message plus the request id to quote in a support ticket. This prevents
internal details (exception types, file paths, library internals) from leaking to callers.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .logging_config import request_id_var
from .services.resilience import CircuitOpenError

logger = logging.getLogger("cat.errors")


async def circuit_open_handler(request: Request, exc: CircuitOpenError) -> JSONResponse:
    logger.warning("Circuit open on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=503,
        content={
            "detail": "Azure is temporarily unavailable. Please retry shortly.",
            "request_id": request_id_var.get(),
        },
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "detail": "An unexpected error occurred. Please try again later.",
            "request_id": request_id_var.get(),
        },
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(CircuitOpenError, circuit_open_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
