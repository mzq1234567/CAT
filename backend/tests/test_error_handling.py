"""Global error handlers must never leak internals to the client (Step 8 / security)."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.errors import register_error_handlers
from app.services.resilience import CircuitOpenError


def _app_that_raises(exc: Exception) -> TestClient:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/boom")
    def boom():
        raise exc

    # raise_server_exceptions=False so the registered handler runs (like a real deployment).
    return TestClient(app, raise_server_exceptions=False)


def test_unhandled_exception_returns_generic_500_without_leaking():
    secret = "SENSITIVE_CONNECTION_STRING=Server=prod;Password=hunter2"
    client = _app_that_raises(RuntimeError(secret))
    r = client.get("/boom")
    assert r.status_code == 500
    body = r.text
    assert secret not in body
    assert "RuntimeError" not in body
    assert "Traceback" not in body
    assert r.json()["detail"] == "An unexpected error occurred. Please try again later."
    assert "request_id" in r.json()


def test_circuit_open_returns_503():
    client = _app_that_raises(CircuitOpenError("circuit 'azure-arm' is open"))
    r = client.get("/boom")
    assert r.status_code == 503
    assert "temporarily unavailable" in r.json()["detail"].lower()
    assert "azure-arm" not in r.text  # internal breaker name not leaked


def test_main_app_registers_handlers():
    import app.main as main
    assert Exception in main.app.exception_handlers
    assert CircuitOpenError in main.app.exception_handlers
