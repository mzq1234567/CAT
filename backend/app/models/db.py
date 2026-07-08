from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, JSON, Float, ForeignKey, Text
from sqlalchemy.orm import relationship
from ..database import Base


class Assessment(Base):
    __tablename__ = "assessments"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, index=True)
    user_email = Column(String)
    subscription_ids = Column(JSON)
    status = Column(String, default="pending")
    error_message = Column(Text, nullable=True)
    total_savings_monthly = Column(Float, default=0.0)
    total_savings_annual = Column(Float, default=0.0)
    findings_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
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
    description = Column(Text)
    recommendation = Column(Text)
    details = Column(JSON, nullable=True)

    assessment = relationship("Assessment", back_populates="findings")


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
