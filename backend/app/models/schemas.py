import re
from pydantic import BaseModel, field_validator
from datetime import datetime
from typing import Optional, List, Dict, Any

# Azure subscription IDs are GUIDs — validate format to reject junk before it reaches ARM URLs.
_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_MAX_SUBSCRIPTIONS = 50


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
    confidence: float = 0.0
    description: str
    recommendation: str
    advisor_recommendation_id: Optional[str] = None
    validation_status: Optional[str] = None
    validation_variance_pct: Optional[float] = None
    actual_monthly_cost: Optional[float] = None
    dismissed: bool = False
    # DEV-ONLY; null unless DEBUG_FINDINGS_REASONING is enabled.
    debug_reason: Optional[str] = None
    details: Optional[Dict[str, Any]] = None

    class Config:
        from_attributes = True


class AssessmentCreate(BaseModel):
    subscription_ids: List[str]

    @field_validator("subscription_ids")
    @classmethod
    def _validate_subscription_ids(cls, value: List[str]) -> List[str]:
        if not value:
            raise ValueError("At least one subscription ID is required.")
        if len(value) > _MAX_SUBSCRIPTIONS:
            raise ValueError(f"Too many subscriptions (max {_MAX_SUBSCRIPTIONS}).")
        cleaned = []
        seen = set()
        for sub in value:
            sub = (sub or "").strip()
            if not _GUID_RE.match(sub):
                raise ValueError(f"Invalid subscription ID format: {sub!r}")
            if sub.lower() not in seen:
                seen.add(sub.lower())
                cleaned.append(sub)
        return cleaned


class AssessmentSummary(BaseModel):
    id: int
    user_email: str
    subscription_ids: List[str]
    status: str
    progress: int = 0
    status_message: Optional[str] = None
    total_savings_monthly: float
    total_savings_annual: float
    findings_count: int
    needs_review_count: int = 0
    total_resources: Optional[int] = None
    resource_type_count: Optional[int] = None
    # Actual spend (null when billing/Cost Management access is unavailable)
    current_monthly_spend: Optional[float] = None
    current_annual_spend: Optional[float] = None
    spend_by_area: Optional[Dict[str, float]] = None
    cost_data_available: bool = False
    currency: str = "USD"
    # Annual spend-growth rate from the recent trend (e.g. 0.12 = +12%/yr); null if too little history.
    observed_annual_growth: Optional[float] = None
    error_message: Optional[str] = None
    created_at: datetime
    snapshot_at: Optional[datetime] = None
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
