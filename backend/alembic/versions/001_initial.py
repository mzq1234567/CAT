"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-06-16
"""
from alembic import op
import sqlalchemy as sa

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "assessments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.String(), index=True),
        sa.Column("user_email", sa.String()),
        sa.Column("subscription_ids", sa.JSON()),
        sa.Column("status", sa.String(), default="pending"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("total_savings_monthly", sa.Float(), default=0.0),
        sa.Column("total_savings_annual", sa.Float(), default=0.0),
        sa.Column("findings_count", sa.Integer(), default=0),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "findings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("assessment_id", sa.Integer(), sa.ForeignKey("assessments.id")),
        sa.Column("category", sa.String(), index=True),
        sa.Column("display_name", sa.String()),
        sa.Column("resource_id", sa.String(), nullable=True),
        sa.Column("resource_name", sa.String(), nullable=True),
        sa.Column("subscription_id", sa.String(), nullable=True),
        sa.Column("resource_group", sa.String(), nullable=True),
        sa.Column("resource_type", sa.String(), nullable=True),
        sa.Column("estimated_savings_monthly", sa.Float(), default=0.0),
        sa.Column("estimated_savings_annual", sa.Float(), default=0.0),
        sa.Column("severity", sa.String(), default="medium"),
        sa.Column("description", sa.Text()),
        sa.Column("recommendation", sa.Text()),
        sa.Column("details", sa.JSON(), nullable=True),
    )
    op.create_table(
        "inventory_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("assessment_id", sa.Integer(), sa.ForeignKey("assessments.id")),
        sa.Column("subscription_id", sa.String()),
        sa.Column("resource_id", sa.String()),
        sa.Column("resource_type", sa.String()),
        sa.Column("resource_name", sa.String()),
        sa.Column("location", sa.String(), nullable=True),
        sa.Column("resource_group", sa.String(), nullable=True),
        sa.Column("data", sa.JSON()),
    )


def downgrade():
    op.drop_table("inventory_items")
    op.drop_table("findings")
    op.drop_table("assessments")
