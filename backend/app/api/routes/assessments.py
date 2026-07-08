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
from ...services.assessment import run_assessment

router = APIRouter()


@router.post("/", response_model=AssessmentSummary, status_code=202)
async def create_assessment(
    body: AssessmentCreate,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not body.subscription_ids:
        raise HTTPException(status_code=400, detail="At least one subscription ID is required.")

    assessment = Assessment(
        user_id=user["user_id"],
        user_email=user["email"],
        subscription_ids=body.subscription_ids,
        status="pending",
    )
    db.add(assessment)
    db.commit()
    db.refresh(assessment)

    background_tasks.add_task(run_assessment, assessment.id, body.subscription_ids, user["token"])
    return assessment


@router.get("/", response_model=List[AssessmentSummary])
def list_assessments(
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return (
        db.query(Assessment)
        .filter(Assessment.user_id == user["user_id"])
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
    assessment = db.get(Assessment, assessment_id)
    if not assessment or assessment.user_id != user["user_id"]:
        raise HTTPException(status_code=404, detail="Assessment not found.")
    return assessment


@router.get("/{assessment_id}/findings", response_model=List[FindingResponse])
def get_findings(
    assessment_id: int,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    assessment = db.get(Assessment, assessment_id)
    if not assessment or assessment.user_id != user["user_id"]:
        raise HTTPException(status_code=404, detail="Assessment not found.")
    return db.query(Finding).filter(Finding.assessment_id == assessment_id).all()


@router.get("/{assessment_id}/findings/by-category", response_model=List[FindingsByCategoryResponse])
def get_findings_by_category(
    assessment_id: int,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    assessment = db.get(Assessment, assessment_id)
    if not assessment or assessment.user_id != user["user_id"]:
        raise HTTPException(status_code=404, detail="Assessment not found.")

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

    assessment = db.get(Assessment, assessment_id)
    if not assessment or assessment.user_id != user["user_id"]:
        raise HTTPException(status_code=404, detail="Assessment not found.")
    if assessment.status != "completed":
        raise HTTPException(status_code=400, detail="Assessment is not yet completed.")

    findings = db.query(Finding).filter(Finding.assessment_id == assessment_id).all()
    pdf_bytes = generate_pdf(assessment, findings)
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

    assessment = db.get(Assessment, assessment_id)
    if not assessment or assessment.user_id != user["user_id"]:
        raise HTTPException(status_code=404, detail="Assessment not found.")
    if assessment.status != "completed":
        raise HTTPException(status_code=400, detail="Assessment is not yet completed.")

    findings = db.query(Finding).filter(Finding.assessment_id == assessment_id).all()
    xlsx_bytes = generate_excel(assessment, findings)
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="assessment-{assessment_id}.xlsx"'},
    )
