from datetime import datetime
from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from ...api.dependencies import get_current_user
from ...database import get_db
from ...models.db import Assessment, Finding
from ...models.schemas import (
    AssessmentCreate,
    AssessmentResponse,
    AssessmentSummary,
    FindingResponse,
    FindingsByCategoryResponse,
)
from ...security.rate_limit import enforce_assessment_rate_limit
from ...security.rbac import verify_subscription_access
from ...services.assessment import run_assessment
from ...services.audit import (
    ASSESSMENT_RUN,
    FINDING_DISMISSED,
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
    _owned_assessment(db, assessment_id, user)
    finding = db.get(Finding, finding_id)
    if not finding or finding.assessment_id != assessment_id:
        raise HTTPException(status_code=404, detail="Finding not found.")
    finding.dismissed = 1
    finding.dismissed_by = user["email"]
    finding.dismissed_at = datetime.utcnow()
    db.commit()
    db.refresh(finding)
    record_audit(db, FINDING_DISMISSED, user, resource=f"finding:{finding_id}",
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


@router.get("/{assessment_id}/report/excel")
def download_excel(
    assessment_id: int,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from ...services.report import generate_excel

    assessment = _owned_assessment(db, assessment_id, user)
    if assessment.status != "completed":
        raise HTTPException(status_code=400, detail="Assessment is not yet completed.")

    findings = db.query(Finding).filter(Finding.assessment_id == assessment_id).all()
    xlsx_bytes = generate_excel(assessment, findings)
    record_audit(db, REPORT_DOWNLOADED, user, resource=f"assessment:{assessment_id}", format="excel")
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="assessment-{assessment_id}.xlsx"'},
    )
