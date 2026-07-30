"""
Audit logging (Step 7).

Every security-relevant action — assessment run, finding dismissal, report download — is written to
the immutable `audit_logs` table AND emitted as a structured log line. The DB record is the durable
compliance trail; the log line is for real-time observability.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from ..logging_config import request_id_var
from ..models.db import AuditLog

logger = logging.getLogger("cat.audit")

# Event names
ASSESSMENT_RUN = "assessment_run"
FINDING_DISMISSED = "finding_dismissed"
REPORT_DOWNLOADED = "report_downloaded"


def record_audit(
    db: Session, event: str, user: Dict[str, Any],
    resource: Optional[str] = None, **detail: Any,
) -> None:
    request_id = request_id_var.get()
    entry = AuditLog(
        event=event,
        user_id=user.get("user_id"),
        tenant_id=user.get("tenant_id"),
        user_email=user.get("email"),
        resource=resource,
        request_id=request_id if request_id != "-" else None,
        detail=detail or None,
    )
    db.add(entry)
    db.commit()
    logger.info(
        "audit",
        extra={"extra_fields": {"event": event, "resource": resource, **detail}},
    )
