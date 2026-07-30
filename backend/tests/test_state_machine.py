"""Tests for the assessment state machine + progress tracker (Step 5)."""
from __future__ import annotations

from app.models.db import Assessment
from app.services.state_machine import (
    PIPELINE,
    PROGRESS,
    AssessmentState,
    ProgressTracker,
    is_valid_transition,
    next_state,
)


# ── State machine (pure) ────────────────────────────────────────────────────────

def test_pipeline_is_ordered_and_complete():
    assert PIPELINE[0] == AssessmentState.QUEUED
    assert PIPELINE[-1] == AssessmentState.COMPLETED
    assert AssessmentState.FETCHING_METRICS in PIPELINE
    assert AssessmentState.FAILED not in PIPELINE


def test_next_state_walks_pipeline():
    assert next_state(AssessmentState.QUEUED) == AssessmentState.FETCHING_RESOURCES
    assert next_state(AssessmentState.GENERATING_REPORT) == AssessmentState.COMPLETED
    assert next_state(AssessmentState.COMPLETED) is None
    assert next_state(AssessmentState.FAILED) is None


def test_progress_is_monotonic_non_decreasing():
    values = [PROGRESS[s] for s in PIPELINE]
    assert values == sorted(values)
    assert values[0] == 0
    assert values[-1] == 100


def test_valid_transitions():
    assert is_valid_transition(AssessmentState.QUEUED, AssessmentState.FETCHING_RESOURCES)
    assert is_valid_transition(AssessmentState.QUEUED, AssessmentState.DETECTING_FINDINGS)  # forward skip ok
    assert is_valid_transition(AssessmentState.RUNNING_ADVISOR, AssessmentState.FAILED)


def test_invalid_transitions():
    assert not is_valid_transition(AssessmentState.FETCHING_METRICS, AssessmentState.QUEUED)  # backward
    assert not is_valid_transition(AssessmentState.COMPLETED, AssessmentState.FAILED)          # terminal
    assert not is_valid_transition(AssessmentState.COMPLETED, AssessmentState.GENERATING_REPORT)


# ── ProgressTracker (DB) ────────────────────────────────────────────────────────

def _make_assessment(db_session) -> int:
    a = Assessment(user_id="u1", user_email="u@x.com", subscription_ids=["sub-1"], status="queued")
    db_session.add(a)
    db_session.commit()
    db_session.refresh(a)
    return a.id


def test_tracker_advance_updates_row(db_session):
    aid = _make_assessment(db_session)
    tracker = ProgressTracker(db_session, aid)

    tracker.advance(AssessmentState.FETCHING_RESOURCES)
    a = db_session.get(Assessment, aid)
    assert a.status == "fetching_resources"
    assert a.progress == 15
    assert a.status_message == "Collecting Azure resource inventory"
    # snapshot_at ("as of" time) stamped when resource fetching starts.
    assert a.snapshot_at is not None


def test_tracker_snapshot_at_set_once(db_session):
    aid = _make_assessment(db_session)
    tracker = ProgressTracker(db_session, aid)
    tracker.advance(AssessmentState.FETCHING_RESOURCES)
    first_snapshot = db_session.get(Assessment, aid).snapshot_at
    tracker.advance(AssessmentState.FETCHING_METRICS)
    # advancing further must not move the "as of" time
    assert db_session.get(Assessment, aid).snapshot_at == first_snapshot


def test_tracker_complete_sets_completed_at(db_session):
    aid = _make_assessment(db_session)
    tracker = ProgressTracker(db_session, aid)
    tracker.advance(AssessmentState.COMPLETED)
    a = db_session.get(Assessment, aid)
    assert a.progress == 100
    assert a.completed_at is not None


def test_tracker_fail_records_error(db_session):
    aid = _make_assessment(db_session)
    tracker = ProgressTracker(db_session, aid)
    tracker.advance(AssessmentState.FETCHING_RESOURCES)
    tracker.fail("boom: token expired")
    a = db_session.get(Assessment, aid)
    assert a.status == "failed"
    assert a.error_message == "boom: token expired"
    assert a.completed_at is not None
