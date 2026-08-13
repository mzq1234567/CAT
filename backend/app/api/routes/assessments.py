import re
from datetime import datetime
from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from ...api.dependencies import get_current_user
from ...database import get_db
from ...models.db import Assessment, Finding

_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
from ...models.schemas import (
    AssessmentCreate,
    AssessmentResponse,
    AssessmentSummary,
    FindingResponse,
    FindingsByCategoryResponse,
    PreflightResponse,
)
from ...security.rate_limit import enforce_assessment_rate_limit
from ...security.rbac import verify_subscription_access
from ...services.assessment import run_assessment
from ...services.preflight import run_preflight
from ...services.findings import CONDITIONAL_CATEGORIES
from ...services.audit import (
    ASSESSMENT_RUN,
    FINDING_DISMISSED,
    FINDING_RESTORED,
    REPORT_DOWNLOADED,
    record_audit,
)
from ...services.azure_client import AzureClient

router = APIRouter()


def _owned_assessment(db: Session, assessment_id: int, user: dict) -> Assessment:
    """Fetch an assessment, enforcing tenant + user isolation. 404 if not owned."""
    assessment = db.get(Assessment, assessment_id)
    if (
        not assessment
        or assessment.user_id != user["user_id"]
        or (assessment.tenant_id or "") != (user.get("tenant_id") or "")
    ):
        raise HTTPException(status_code=404, detail="Assessment not found.")
    return assessment


@router.get("/preflight", response_model=PreflightResponse)
async def preflight(
    subscription_id: str = Query(..., description="Subscription to check readiness for"),
    user: dict = Depends(get_current_user),
):
    """Self-service readiness check for a subscription — probes each data source (subscription access,
    resource inventory, cost, metrics, pricing) in the signed-in user's context so missing access is
    surfaced BEFORE running an assessment. Client-safe: no raw HTTP/SDK errors are returned."""
    if not _GUID_RE.match(subscription_id or ""):
        raise HTTPException(status_code=400, detail="Invalid subscription id.")
    client = AzureClient(user["token"])
    return await run_preflight(client, subscription_id, user_email=user.get("email"))


@router.post("/", response_model=AssessmentSummary, status_code=202)
async def create_assessment(
    body: AssessmentCreate,
    background_tasks: BackgroundTasks,
    user: dict = Depends(enforce_assessment_rate_limit),
    db: Session = Depends(get_db),
):
    if not body.subscription_ids:
        raise HTTPException(status_code=400, detail="At least one subscription ID is required.")

    # RBAC: confirm the caller has Reader access to every requested subscription.
    client = AzureClient(user["token"])
    await verify_subscription_access(client, body.subscription_ids)

    assessment = Assessment(
        user_id=user["user_id"],
        user_email=user["email"],
        tenant_id=user.get("tenant_id"),
        subscription_ids=body.subscription_ids,
        status="queued",
        progress=0,
        status_message="Queued for processing",
    )
    db.add(assessment)
    db.commit()
    db.refresh(assessment)

    record_audit(db, ASSESSMENT_RUN, user, resource=f"assessment:{assessment.id}",
                 subscription_ids=body.subscription_ids)

    background_tasks.add_task(run_assessment, assessment.id, body.subscription_ids, user["token"])
    return assessment


@router.get("/", response_model=List[AssessmentSummary])
def list_assessments(
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return (
        db.query(Assessment)
        .filter(
            Assessment.user_id == user["user_id"],
            Assessment.tenant_id == user.get("tenant_id"),
        )
        .order_by(Assessment.created_at.desc())
        .limit(20)
        .all()
    )


@router.get("/{assessment_id}", response_model=AssessmentResponse)
def get_assessment(
    assessment_id: int,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _owned_assessment(db, assessment_id, user)


@router.get("/{assessment_id}/findings", response_model=List[FindingResponse])
def get_findings(
    assessment_id: int,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _owned_assessment(db, assessment_id, user)
    return db.query(Finding).filter(Finding.assessment_id == assessment_id).all()


@router.post("/{assessment_id}/findings/{finding_id}/dismiss", response_model=FindingResponse)
def dismiss_finding(
    assessment_id: int,
    finding_id: int,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    assessment = _owned_assessment(db, assessment_id, user)
    finding = db.get(Finding, finding_id)
    if not finding or finding.assessment_id != assessment_id:
        raise HTTPException(status_code=404, detail="Finding not found.")
    finding.dismissed = 1
    finding.dismissed_by = user["email"]
    finding.dismissed_at = datetime.utcnow()

    # Re-roll the headline totals from the surviving findings so the dashboard stays consistent
    # (a dismissed opportunity no longer counts toward savings). Mirrors the initial roll-up.
    active = [f for f in assessment.findings if not f.dismissed]
    # Headline totals count only QUANTIFIED, non-conditional savings — conditional AHB and REVIEW
    # ("not quantified") findings are surfaced separately and never folded into the total (mirrors the
    # initial roll-up in _persist_findings_and_totals).
    realisable = [
        f for f in active
        if f.category not in CONDITIONAL_CATEGORIES and (f.evidence_state or "quantified") == "quantified"
    ]
    # Sum the NON-OVERLAPPING contribution (counted_savings) so RI + right-sizing on the same VM never
    # double-count. counted defaults to estimated for non-overlapping findings.
    def _counted_m(f):
        return f.counted_savings_monthly if f.counted_savings_monthly is not None else f.estimated_savings_monthly
    def _counted_a(f):
        return f.counted_savings_annual if f.counted_savings_annual is not None else f.estimated_savings_annual
    assessment.total_savings_monthly = round(sum(_counted_m(f) for f in realisable), 2)
    assessment.total_savings_annual = round(sum(_counted_a(f) for f in realisable), 2)
    assessment.findings_count = len(active)

    db.commit()
    db.refresh(finding)
    record_audit(db, FINDING_DISMISSED, user, resource=f"finding:{finding_id}",
                 assessment_id=assessment_id)
    return finding


@router.post("/{assessment_id}/findings/{finding_id}/restore", response_model=FindingResponse)
def restore_finding(
    assessment_id: int,
    finding_id: int,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Undo an exclusion — bring a previously-excluded recommendation back into the assessment and its
    savings totals. This is what makes the exclude action safe/reversible (no data is ever lost)."""
    assessment = _owned_assessment(db, assessment_id, user)
    finding = db.get(Finding, finding_id)
    if not finding or finding.assessment_id != assessment_id:
        raise HTTPException(status_code=404, detail="Finding not found.")
    finding.dismissed = 0
    finding.dismissed_by = None
    finding.dismissed_at = None

    active = [f for f in assessment.findings if not f.dismissed]
    # Headline totals count only QUANTIFIED, non-conditional savings — conditional AHB and REVIEW
    # ("not quantified") findings are surfaced separately and never folded into the total (mirrors the
    # initial roll-up in _persist_findings_and_totals).
    realisable = [
        f for f in active
        if f.category not in CONDITIONAL_CATEGORIES and (f.evidence_state or "quantified") == "quantified"
    ]
    # Sum the NON-OVERLAPPING contribution (counted_savings) so RI + right-sizing on the same VM never
    # double-count. counted defaults to estimated for non-overlapping findings.
    def _counted_m(f):
        return f.counted_savings_monthly if f.counted_savings_monthly is not None else f.estimated_savings_monthly
    def _counted_a(f):
        return f.counted_savings_annual if f.counted_savings_annual is not None else f.estimated_savings_annual
    assessment.total_savings_monthly = round(sum(_counted_m(f) for f in realisable), 2)
    assessment.total_savings_annual = round(sum(_counted_a(f) for f in realisable), 2)
    assessment.findings_count = len(active)

    db.commit()
    db.refresh(finding)
    record_audit(db, FINDING_RESTORED, user, resource=f"finding:{finding_id}",
                 assessment_id=assessment_id)
    return finding


@router.get("/{assessment_id}/findings/by-category", response_model=List[FindingsByCategoryResponse])
def get_findings_by_category(
    assessment_id: int,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _owned_assessment(db, assessment_id, user)

    rows = (
        db.query(
            Finding.category,
            Finding.display_name,
            func.count(Finding.id).label("count"),
            func.sum(Finding.estimated_savings_monthly).label("total_monthly"),
            func.sum(Finding.estimated_savings_annual).label("total_annual"),
        )
        .filter(Finding.assessment_id == assessment_id)
        .group_by(Finding.category, Finding.display_name)
        .order_by(func.sum(Finding.estimated_savings_monthly).desc())
        .all()
    )
    return [
        FindingsByCategoryResponse(
            category=r.category,
            display_name=r.display_name,
            count=r.count,
            total_monthly=round(r.total_monthly or 0, 2),
            total_annual=round(r.total_annual or 0, 2),
        )
        for r in rows
    ]


@router.get("/{assessment_id}/report/pdf")
def download_pdf(
    assessment_id: int,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from ...services.report import generate_pdf

    assessment = _owned_assessment(db, assessment_id, user)
    if assessment.status != "completed":
        raise HTTPException(status_code=400, detail="Assessment is not yet completed.")

    findings = db.query(Finding).filter(Finding.assessment_id == assessment_id).all()
    pdf_bytes = generate_pdf(assessment, findings)
    record_audit(db, REPORT_DOWNLOADED, user, resource=f"assessment:{assessment_id}", format="pdf")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="assessment-{assessment_id}.pdf"'},
    )
