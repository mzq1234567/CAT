"""
Assessment state machine + progress tracking (Step 5).

Replaces the flat pending/running/completed/failed status with an explicit pipeline so the
frontend can show real progress and reports can state a trustworthy "as of" snapshot time.

Pipeline (ordered):
    QUEUED → FETCHING_RESOURCES → FETCHING_METRICS → RUNNING_ADVISOR
           → CALCULATING_PRICES → DETECTING_FINDINGS → GENERATING_REPORT → COMPLETED
Any state may transition to FAILED.

`snapshot_at` is stamped when resource fetching begins — it is the moment the inventory reflects,
so a report can say "as of <snapshot_at>" and a reader knows the data may have drifted since.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Dict, Optional

from sqlalchemy.orm import Session

from ..models.db import Assessment


class AssessmentState(str, Enum):
    QUEUED = "queued"
    FETCHING_RESOURCES = "fetching_resources"
    FETCHING_METRICS = "fetching_metrics"
    RUNNING_ADVISOR = "running_advisor"
    CALCULATING_PRICES = "calculating_prices"
    DETECTING_FINDINGS = "detecting_findings"
    GENERATING_REPORT = "generating_report"
    COMPLETED = "completed"
    FAILED = "failed"


# Ordered happy-path pipeline (excludes FAILED).
PIPELINE = [
    AssessmentState.QUEUED,
    AssessmentState.FETCHING_RESOURCES,
    AssessmentState.FETCHING_METRICS,
    AssessmentState.RUNNING_ADVISOR,
    AssessmentState.CALCULATING_PRICES,
    AssessmentState.DETECTING_FINDINGS,
    AssessmentState.GENERATING_REPORT,
    AssessmentState.COMPLETED,
]

TERMINAL = {AssessmentState.COMPLETED, AssessmentState.FAILED}

# Progress % surfaced to the frontend polling loop.
PROGRESS: Dict[AssessmentState, int] = {
    AssessmentState.QUEUED: 0,
    AssessmentState.FETCHING_RESOURCES: 15,
    AssessmentState.FETCHING_METRICS: 35,
    AssessmentState.RUNNING_ADVISOR: 55,
    AssessmentState.CALCULATING_PRICES: 70,
    AssessmentState.DETECTING_FINDINGS: 85,
    AssessmentState.GENERATING_REPORT: 95,
    AssessmentState.COMPLETED: 100,
    AssessmentState.FAILED: 100,
}

MESSAGES: Dict[AssessmentState, str] = {
    AssessmentState.QUEUED: "Queued for processing",
    AssessmentState.FETCHING_RESOURCES: "Collecting Azure resource inventory",
    AssessmentState.FETCHING_METRICS: "Fetching utilisation metrics",
    AssessmentState.RUNNING_ADVISOR: "Retrieving Azure Advisor recommendations",
    AssessmentState.CALCULATING_PRICES: "Calculating live prices",
    AssessmentState.DETECTING_FINDINGS: "Detecting cost optimisation findings",
    AssessmentState.GENERATING_REPORT: "Finalising results",
    AssessmentState.COMPLETED: "Assessment complete",
    AssessmentState.FAILED: "Assessment failed",
}


def next_state(state: AssessmentState) -> Optional[AssessmentState]:
    """Return the next pipeline state, or None if terminal/unknown."""
    if state in TERMINAL or state not in PIPELINE:
        return None
    idx = PIPELINE.index(state)
    return PIPELINE[idx + 1] if idx + 1 < len(PIPELINE) else None


def is_valid_transition(frm: AssessmentState, to: AssessmentState) -> bool:
    """Forward moves along the pipeline are valid; any state may fail."""
    if to == AssessmentState.FAILED:
        return frm not in TERMINAL
    if frm in TERMINAL or frm not in PIPELINE or to not in PIPELINE:
        return False
    return PIPELINE.index(to) > PIPELINE.index(frm)


class ProgressTracker:
    """Persists state-machine transitions onto an Assessment row.

    Not thread-safe by design — one tracker drives one assessment's background task.
    """

    def __init__(self, db: Session, assessment_id: int):
        self._db = db
        self._id = assessment_id
        self.state = AssessmentState.QUEUED

    def advance(self, state: AssessmentState, message: Optional[str] = None) -> None:
        assessment = self._db.get(Assessment, self._id)
        if assessment is None:
            return
        self.state = state
        assessment.status = state.value
        assessment.progress = PROGRESS.get(state, 0)
        assessment.status_message = message or MESSAGES.get(state, "")
        # Stamp the inventory "as of" time when we start collecting resources.
        if state == AssessmentState.FETCHING_RESOURCES and assessment.snapshot_at is None:
            assessment.snapshot_at = datetime.utcnow()
        if state in TERMINAL:
            assessment.completed_at = datetime.utcnow()
        self._db.commit()

    def fail(self, error_message: str) -> None:
        assessment = self._db.get(Assessment, self._id)
        if assessment is None:
            return
        self.state = AssessmentState.FAILED
        assessment.status = AssessmentState.FAILED.value
        assessment.progress = PROGRESS[AssessmentState.FAILED]
        assessment.status_message = MESSAGES[AssessmentState.FAILED]
        assessment.error_message = error_message
        assessment.completed_at = datetime.utcnow()
        self._db.commit()
