from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, JSON, Float, ForeignKey, Text
from sqlalchemy.orm import relationship
from ..database import Base


class Assessment(Base):
    __tablename__ = "assessments"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, index=True)
    user_email = Column(String)
    tenant_id = Column(String, index=True, nullable=True)  # tenant isolation (Step 7)
    tenant_display_name = Column(String, nullable=True)     # client name for the report cover
    subscription_ids = Column(JSON)
    subscription_names = Column(JSON, nullable=True)        # {subscription_id: display_name}
    major_resource_types = Column(JSON, nullable=True)      # [{"type": .., "count": ..}] top types
    status = Column(String, default="queued")  # AssessmentState value (Step 5)
    progress = Column(Integer, default=0)  # 0–100 for frontend polling (Step 5)
    status_message = Column(String, nullable=True)  # human-readable phase label (Step 5)
    error_message = Column(Text, nullable=True)
    total_savings_monthly = Column(Float, default=0.0)
    total_savings_annual = Column(Float, default=0.0)
    findings_count = Column(Integer, default=0)
    needs_review_count = Column(Integer, default=0)  # validation flags (Step 4/6)
    total_resources = Column(Integer, nullable=True)  # full ARG inventory count
    resource_type_count = Column(Integer, nullable=True)  # distinct resource types scanned
    # Actual spend from Cost Management (nullable — null when billing access is unavailable).
    current_monthly_spend = Column(Float, nullable=True)
    current_annual_spend = Column(Float, nullable=True)
    spend_by_area = Column(JSON, nullable=True)  # {area: monthly_cost}
    cost_data_available = Column(Integer, default=0)  # 0/1 — did we get any cost data?
    # Set when the SUBSCRIPTION-level spend was obtained but the PER-RESOURCE billed-cost detail was NOT
    # (Cost Management throttled the heavier per-resource query). In that state grounded findings can't be
    # quantified, so ungrounded list-price findings are withheld and the UI shows a "re-run" banner. 0/1.
    billing_detail_unavailable = Column(Integer, default=0)
    # Azure data-collection quality: "complete" | "partial" | "failed". PARTIAL/FAILED means some Azure
    # data could not be collected (throttle/timeout/error after retries) — missing data is NOT zero, so
    # such a run must never present as a clean complete assessment. `data_quality_message` is the concise
    # client-facing line; `collection_diagnostics` is the detailed internal record (for logs/debug).
    data_quality = Column(String, default="complete")
    data_quality_message = Column(String, nullable=True)
    collection_diagnostics = Column(JSON, nullable=True)
    # Spend is an ESTIMATED run rate (no complete billing month — new/migrated sub) rather than a real
    # last-month bill. `spend_period_days` = days of billing the estimate was averaged over.
    spend_estimated = Column(Integer, default=0)  # 0/1
    spend_period_days = Column(Integer, nullable=True)
    currency = Column(String, default="USD")  # billing currency (from Cost Management)
    # Annual spend-growth rate from a best-fit line through recent monthly spend (e.g. 0.12 = +12%/yr).
    # Drives the report's Linear/Conservative growth projections; null when there's too little history.
    observed_annual_growth = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    snapshot_at = Column(DateTime, nullable=True)  # "as of" inventory time (Step 5)
    completed_at = Column(DateTime, nullable=True)

    findings = relationship("Finding", back_populates="assessment", cascade="all, delete-orphan")
    inventory_items = relationship("InventoryItem", back_populates="assessment", cascade="all, delete-orphan")
    events = relationship(
        "AssessmentEvent", back_populates="assessment", cascade="all, delete-orphan",
        order_by="AssessmentEvent.id",
    )


class Finding(Base):
    __tablename__ = "findings"

    id = Column(Integer, primary_key=True, index=True)
    assessment_id = Column(Integer, ForeignKey("assessments.id"))
    category = Column(String, index=True)
    display_name = Column(String)
    resource_id = Column(String, nullable=True)
    resource_name = Column(String, nullable=True)
    subscription_id = Column(String, nullable=True)
    resource_group = Column(String, nullable=True)
    resource_type = Column(String, nullable=True)
    estimated_savings_monthly = Column(Float, default=0.0)
    estimated_savings_annual = Column(Float, default=0.0)
    # Non-overlapping contribution to Total Identified Savings (RI vs right-sizing de-overlapped). Equals
    # estimated_savings_* unless this finding overlaps another on the same VM's compute; the total sums
    # these, while the UI still displays each finding's own estimated_savings_*. NULL (never set) → callers
    # fall back to estimated_savings_*, so a superseded 0 is distinguishable from an unset value.
    counted_savings_monthly = Column(Float, nullable=True)
    counted_savings_annual = Column(Float, nullable=True)
    severity = Column(String, default="medium")
    confidence = Column(Float, default=0.0)  # 0..1 (Step 6)
    description = Column(Text)
    recommendation = Column(Text)
    # Advisor correlation + Cost Management validation (Steps 4/6)
    advisor_recommendation_id = Column(String, nullable=True)
    validation_status = Column(String, nullable=True)  # validated | needs_review | unvalidated
    validation_variance_pct = Column(Float, nullable=True)
    actual_monthly_cost = Column(Float, nullable=True)
    # Financial evidence state — "quantified" (defensible, countable saving) or "review" (real signal,
    # unquantifiable for this customer → shown as "Not quantified", excluded from every savings total).
    evidence_state = Column(String, default="quantified")
    # DEV-ONLY reasoning; gated by DEBUG_FINDINGS_REASONING.
    # TODO: remove or gate behind admin-only role before prod.
    debug_reason = Column(Text, nullable=True)
    details = Column(JSON, nullable=True)
    # Dismissal (Step 7) — audited.
    dismissed = Column(Integer, default=0)  # 0/1 (SQLite-friendly boolean)
    dismissed_by = Column(String, nullable=True)
    dismissed_at = Column(DateTime, nullable=True)

    assessment = relationship("Assessment", back_populates="findings")


class AuditLog(Base):
    """Immutable record of security-relevant actions (Step 7)."""
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    event = Column(String, index=True)  # assessment_run | finding_dismissed | report_downloaded
    user_id = Column(String, index=True)
    tenant_id = Column(String, index=True, nullable=True)
    user_email = Column(String, nullable=True)
    resource = Column(String, nullable=True)  # e.g. "assessment:12", "finding:34"
    request_id = Column(String, nullable=True)
    detail = Column(JSON, nullable=True)


class AssessmentEvent(Base):
    """A timestamped record of something the pipeline actually did.

    These drive the live event stream on the running-assessment screen. They are written only when
    the pipeline genuinely reaches the corresponding point, and any counts they carry are the real
    values from that run — the UI must never invent progress the backend hasn't made.
    """
    __tablename__ = "assessment_events"

    id = Column(Integer, primary_key=True, index=True)
    assessment_id = Column(Integer, ForeignKey("assessments.id"), index=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    stage = Column(String, nullable=True)  # the AssessmentState this happened in
    message = Column(String)

    assessment = relationship("Assessment", back_populates="events")


class InventoryItem(Base):
    __tablename__ = "inventory_items"

    id = Column(Integer, primary_key=True, index=True)
    assessment_id = Column(Integer, ForeignKey("assessments.id"))
    subscription_id = Column(String)
    resource_id = Column(String)
    resource_type = Column(String)
    resource_name = Column(String)
    location = Column(String, nullable=True)
    resource_group = Column(String, nullable=True)
    data = Column(JSON)

    assessment = relationship("Assessment", back_populates="inventory_items")
