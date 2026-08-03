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
    currency = Column(String, default="USD")  # billing currency (from Cost Management)
    # Annual spend-growth rate from a best-fit line through recent monthly spend (e.g. 0.12 = +12%/yr).
    # Drives the report's Linear/Conservative growth projections; null when there's too little history.
    observed_annual_growth = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    snapshot_at = Column(DateTime, nullable=True)  # "as of" inventory time (Step 5)
    completed_at = Column(DateTime, nullable=True)

    findings = relationship("Finding", back_populates="assessment", cascade="all, delete-orphan")
    inventory_items = relationship("InventoryItem", back_populates="assessment", cascade="all, delete-orphan")


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
    severity = Column(String, default="medium")
    confidence = Column(Float, default=0.0)  # 0..1 (Step 6)
    description = Column(Text)
    recommendation = Column(Text)
    # Advisor correlation + Cost Management validation (Steps 4/6)
    advisor_recommendation_id = Column(String, nullable=True)
    validation_status = Column(String, nullable=True)  # validated | needs_review | unvalidated
    validation_variance_pct = Column(Float, nullable=True)
    actual_monthly_cost = Column(Float, nullable=True)
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
