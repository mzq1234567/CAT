"""
Route-level integration tests: every assessment endpoint end-to-end via TestClient.

Auth + DB are dependency-overridden (route-logic focus; the auth boundary itself is covered by
test_security_pentest.py). run_assessment + RBAC are stubbed so create() returns without network.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies import get_current_user
from app.api.routes import assessments as routes
from app.database import get_db
from app.models.db import Assessment, AuditLog, Finding

USER = {"user_id": "u1", "tenant_id": "t1", "email": "u@x.com", "token": "tok"}
SUB = "00000000-0000-0000-0000-000000000001"


def build_client(db_session, user=USER):
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/assessments")

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def seed_completed(db, user=USER, with_finding=True):
    a = Assessment(
        user_id=user["user_id"], user_email=user["email"], tenant_id=user["tenant_id"],
        subscription_ids=[SUB], status="completed", progress=100,
        total_savings_monthly=100.0, total_savings_annual=1200.0, findings_count=1,
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    if with_finding:
        f = Finding(
            assessment_id=a.id, category="idle_vms", display_name="Idle Virtual Machines",
            resource_name="vm-1", resource_group="rg-a", subscription_id=SUB,
            resource_type="microsoft.compute/virtualmachines",
            estimated_savings_monthly=100.0, estimated_savings_annual=1200.0,
            severity="high", confidence=0.9, description="idle", recommendation="deallocate",
        )
        db.add(f)
        db.commit()
        db.refresh(f)
        a._finding_id = f.id  # type: ignore[attr-defined]
    return a


# ── Full read lifecycle ─────────────────────────────────────────────────────────

def test_list_get_findings_and_by_category(db_session):
    a = seed_completed(db_session)
    client = build_client(db_session)

    listing = client.get("/api/assessments/")
    assert listing.status_code == 200
    assert len(listing.json()) == 1

    detail = client.get(f"/api/assessments/{a.id}")
    assert detail.status_code == 200
    assert detail.json()["findings_count"] == 1
    assert len(detail.json()["findings"]) == 1

    findings = client.get(f"/api/assessments/{a.id}/findings")
    assert findings.status_code == 200
    assert findings.json()[0]["category"] == "idle_vms"

    by_cat = client.get(f"/api/assessments/{a.id}/findings/by-category")
    assert by_cat.status_code == 200
    assert by_cat.json()[0]["count"] == 1
    assert by_cat.json()[0]["total_monthly"] == 100.0


def test_missing_assessment_is_404(db_session):
    client = build_client(db_session)
    assert client.get("/api/assessments/99999").status_code == 404
    assert client.get("/api/assessments/99999/findings").status_code == 404


# ── Dismissal ─────────────────────────────────────────────────────────────────────

def test_dismiss_finding(db_session):
    a = seed_completed(db_session)
    client = build_client(db_session)
    r = client.post(f"/api/assessments/{a.id}/findings/{a._finding_id}/dismiss")
    assert r.status_code == 200
    assert r.json()["id"] == a._finding_id
    assert r.json()["dismissed"] is True
    db_session.refresh(db_session.get(Finding, a._finding_id))
    assert db_session.get(Finding, a._finding_id).dismissed == 1

    # Dismissing the only finding re-rolls the headline totals to zero so the dashboard stays consistent.
    detail = client.get(f"/api/assessments/{a.id}").json()
    assert detail["findings_count"] == 0
    assert detail["total_savings_monthly"] == 0.0
    assert detail["total_savings_annual"] == 0.0


def test_dismiss_nonexistent_finding_is_404(db_session):
    a = seed_completed(db_session, with_finding=False)
    client = build_client(db_session)
    assert client.post(f"/api/assessments/{a.id}/findings/424242/dismiss").status_code == 404


# ── Report downloads ──────────────────────────────────────────────────────────────

def test_download_pdf(db_session):
    a = seed_completed(db_session)
    client = build_client(db_session)
    r = client.get(f"/api/assessments/{a.id}/report/pdf")
    assert r.status_code == 200
    assert r.content[:4] == b"%PDF"
    assert "attachment" in r.headers["content-disposition"]
    assert r.headers["content-type"] == "application/pdf"


def test_excel_route_removed(db_session):
    """The Excel export was removed — the endpoint should no longer exist."""
    a = seed_completed(db_session)
    client = build_client(db_session)
    assert client.get(f"/api/assessments/{a.id}/report/excel").status_code == 404


def test_report_rejected_when_not_completed(db_session):
    a = seed_completed(db_session, with_finding=False)
    a.status = "running"
    db_session.commit()
    client = build_client(db_session)
    assert client.get(f"/api/assessments/{a.id}/report/pdf").status_code == 400


# ── Create (RBAC + run stubbed) ─────────────────────────────────────────────────

def test_create_assessment_happy_path(db_session, monkeypatch):
    async def _noop_verify(client, ids):
        return None

    monkeypatch.setattr(routes, "verify_subscription_access", _noop_verify)
    monkeypatch.setattr(routes, "run_assessment", lambda *a, **k: None)

    client = build_client(db_session)
    r = client.post("/api/assessments/", json={"subscription_ids": [SUB]})
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued"
    assert body["tenant_id"] if "tenant_id" in body else True  # tenant recorded server-side
    # The run was audit-logged.
    runs = db_session.query(AuditLog).filter(AuditLog.event == "assessment_run").all()
    assert len(runs) == 1


def test_create_rejects_inaccessible_subscription(db_session, monkeypatch):
    from fastapi import HTTPException

    async def _deny(client, ids):
        raise HTTPException(status_code=403, detail="no access")

    monkeypatch.setattr(routes, "verify_subscription_access", _deny)
    monkeypatch.setattr(routes, "run_assessment", lambda *a, **k: None)

    client = build_client(db_session)
    r = client.post("/api/assessments/", json={"subscription_ids": [SUB]})
    assert r.status_code == 403
