"""Route-level security tests: tenant isolation, dismissal, audit, rate limiting (Step 7)."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies import get_current_user
from app.api.routes import assessments as routes
from app.database import get_db
from app.models.db import Assessment, AuditLog, Finding
from app.security import rate_limit


USER_A = {"user_id": "userA", "tenant_id": "tenantA", "email": "a@x.com", "token": "tok"}
USER_B = {"user_id": "userB", "tenant_id": "tenantB", "email": "b@x.com", "token": "tok"}


def build_client(db_session, user):
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/assessments")

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _seed_assessment(db, user, status="completed"):
    a = Assessment(
        user_id=user["user_id"], user_email=user["email"], tenant_id=user["tenant_id"],
        subscription_ids=["sub-1"], status=status,
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


# ── Tenant / user isolation ─────────────────────────────────────────────────────

def test_user_cannot_read_other_tenants_assessment(db_session):
    a = _seed_assessment(db_session, USER_B)  # belongs to tenant B
    client = build_client(db_session, USER_A)  # signed in as tenant A
    resp = client.get(f"/api/assessments/{a.id}")
    assert resp.status_code == 404


def test_list_is_scoped_to_tenant(db_session):
    _seed_assessment(db_session, USER_A)
    _seed_assessment(db_session, USER_B)
    client = build_client(db_session, USER_A)
    resp = client.get("/api/assessments/")
    assert resp.status_code == 200
    assert all(item["user_email"] == "a@x.com" for item in resp.json())
    assert len(resp.json()) == 1


# ── Finding dismissal + audit ───────────────────────────────────────────────────

def test_dismiss_finding_marks_and_audits(db_session):
    a = _seed_assessment(db_session, USER_A)
    f = Finding(assessment_id=a.id, category="idle_vms", display_name="Idle VMs",
                description="d", recommendation="r", severity="medium")
    db_session.add(f)
    db_session.commit()
    db_session.refresh(f)

    client = build_client(db_session, USER_A)
    resp = client.post(f"/api/assessments/{a.id}/findings/{f.id}/dismiss")
    assert resp.status_code == 200

    db_session.refresh(f)
    assert f.dismissed == 1
    assert f.dismissed_by == "a@x.com"
    audit = db_session.query(AuditLog).filter(AuditLog.event == "finding_dismissed").all()
    assert len(audit) == 1
    assert audit[0].resource == f"finding:{f.id}"


def test_cannot_dismiss_finding_in_other_tenant(db_session):
    a = _seed_assessment(db_session, USER_B)
    f = Finding(assessment_id=a.id, category="idle_vms", display_name="x",
                description="d", recommendation="r", severity="low")
    db_session.add(f)
    db_session.commit()
    db_session.refresh(f)

    client = build_client(db_session, USER_A)
    resp = client.post(f"/api/assessments/{a.id}/findings/{f.id}/dismiss")
    assert resp.status_code == 404


# ── Rate limiting + RBAC + audit on create ──────────────────────────────────────

def test_create_is_rate_limited(db_session, monkeypatch):
    # Avoid network: stub RBAC + the background task.
    async def _noop_verify(client, ids):
        return None

    monkeypatch.setattr(routes, "verify_subscription_access", _noop_verify)
    monkeypatch.setattr(routes, "run_assessment", lambda *a, **k: None)

    # Tighten the shared limiter for the test.
    rate_limit.assessment_limiter.max_requests = 2
    rate_limit.assessment_limiter._hits.clear()

    client = build_client(db_session, USER_A)
    body = {"subscription_ids": ["00000000-0000-0000-0000-000000000001"]}
    assert client.post("/api/assessments/", json=body).status_code == 202
    assert client.post("/api/assessments/", json=body).status_code == 202
    assert client.post("/api/assessments/", json=body).status_code == 429  # 3rd blocked

    # Two successful creates were audited.
    runs = db_session.query(AuditLog).filter(AuditLog.event == "assessment_run").all()
    assert len(runs) == 2
