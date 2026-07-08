from pydantic import BaseModel
from datetime import datetime
from typing import Optional, List, Dict, Any


class FindingResponse(BaseModel):
    id: int
    category: str
    display_name: str
    resource_id: Optional[str] = None
    resource_name: Optional[str] = None
    subscription_id: Optional[str] = None
    resource_group: Optional[str] = None
    resource_type: Optional[str] = None
    estimated_savings_monthly: float
    estimated_savings_annual: float
    severity: str
    description: str
    recommendation: str
    details: Optional[Dict[str, Any]] = None

    class Config:
        from_attributes = True


class AssessmentCreate(BaseModel):
    subscription_ids: List[str]


class AssessmentSummary(BaseModel):
    id: int
    user_email: str
    subscription_ids: List[str]
    status: str
    total_savings_monthly: float
    total_savings_annual: float
    findings_count: int
    error_message: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class AssessmentResponse(AssessmentSummary):
    findings: List[FindingResponse] = []


class SubscriptionResponse(BaseModel):
    id: str
    display_name: str
    state: str
    tenant_id: str


class FindingsByCategoryResponse(BaseModel):
    category: str
    display_name: str
    count: int
    total_monthly: float
    total_annual: float
