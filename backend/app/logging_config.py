"""
Structured JSON logging + request/user context (Steps 7–8).

Every log line is a single JSON object carrying request_id, user_id and tenant_id (bound per
request via contextvars), so logs from concurrent assessments can be correlated and filtered by
tenant. Call `configure_logging()` once at startup.
"""
from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
user_id_var: ContextVar[str] = ContextVar("user_id", default="-")
tenant_id_var: ContextVar[str] = ContextVar("tenant_id", default="-")

# Standard LogRecord attributes we don't want to duplicate into the JSON payload.
_RESERVED = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime", "extra_fields"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
            "user_id": user_id_var.get(),
            "tenant_id": tenant_id_var.get(),
        }
        # Explicit structured fields passed via logger.info(..., extra={"extra_fields": {...}}).
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        # Any ad-hoc `extra=` keys.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
