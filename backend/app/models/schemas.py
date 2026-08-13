import re
from pydantic import BaseModel, computed_field, field_validator
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
    # Non-overlapping contribution to the total (RI vs right-sizing de-overlapped). Null → callers use
    # estimated_savings_*. The dashboard total sums these; each card still shows its own estimated_savings_*.
    counted_savings_monthly: Optional[float] = None
    counted_savings_annual: Optional[float] = None
    severity: str
    confidence: float = 0.0
    description: str
    recommendation: str
    advisor_recommendation_id: Optional[str] = None
    validation_status: Optional[str] = None
    validation_variance_pct: Optional[float] = None
    actual_monthly_cost: Optional[float] = None
    # Financial evidence state: "quantified" (a defensible, countable saving) or "review" (a real
    # signal we can't price for this customer — shown as "Not quantified", never counted in any total).
    evidence_state: str = "quantified"
    dismissed: bool = False
    # DEV-ONLY; null unless DEBUG_FINDINGS_REASONING is enabled.
    debug_reason: Optional[str] = None
    details: Optional[Dict[str, Any]] = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def basis(self) -> Optional[str]:
        """Client-safe "how this number was calculated" sentence — the single source of truth shared
        with the PDF (see services.finding_basis). Computed from the evidence fields, never stored."""
        from ..services.finding_basis import describe_finding_basis
        return describe_finding_basis(self)

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
    # Real per-type counts from the scan, [{"type": "Virtual Machines", "count": 16}] — surfaced
    # live during the run so discovery metrics show measured values, never invented ones.
    major_resource_types: Optional[List[Dict[str, Any]]] = None
    # Actual spend (null when billing/Cost Management access is unavailable)
    current_monthly_spend: Optional[float] = None
    current_annual_spend: Optional[float] = None
    spend_by_area: Optional[Dict[str, float]] = None
    cost_data_available: bool = False
    # Per-resource billed cost was unavailable this run (Cost Management throttled) though the
    # subscription total came through — grounded findings were withheld; the UI shows a "re-run" banner.
    billing_detail_unavailable: bool = False
    # Azure data-collection quality: "complete" | "partial" | "failed". `data_quality_message` is the
    # concise client-facing line (detailed diagnostics stay internal, not exposed here).
    data_quality: str = "complete"
    data_quality_message: Optional[str] = None
    # Spend is an estimated run rate (new/migrated sub, no complete billing month) over N days.
    spend_estimated: bool = False
    spend_period_days: Optional[int] = None
    currency: str = "USD"
    # Annual spend-growth rate from the recent trend (e.g. 0.12 = +12%/yr); null if too little history.
    observed_annual_growth: Optional[float] = None
    error_message: Optional[str] = None
    created_at: datetime
    snapshot_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class AssessmentEventResponse(BaseModel):
    """One thing the pipeline actually did, for the live event stream."""
    id: int
    timestamp: datetime
    stage: Optional[str] = None
    message: str

    class Config:
        from_attributes = True


class AssessmentResponse(AssessmentSummary):
    findings: List[FindingResponse] = []
    events: List[AssessmentEventResponse] = []


class PreflightCheck(BaseModel):
    key: str
    label: str
    status: str            # "ok" | "warning" | "unavailable"
    detail: str = ""
    blocking: bool = False


class PreflightResponse(BaseModel):
    subscription_id: str
    subscription_name: Optional[str] = None
    tenant_id: Optional[str] = None
    ready: bool
    checks: List[PreflightCheck]


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
