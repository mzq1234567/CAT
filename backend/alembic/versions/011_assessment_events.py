"""live assessment event stream

Revision ID: 011
Revises: 010
Create Date: 2026-08-06
"""
from alembic import op
import sqlalchemy as sa

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "assessment_events",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("assessment_id", sa.Integer(), sa.ForeignKey("assessments.id"), index=True),
        sa.Column("timestamp", sa.DateTime(), nullable=True),
        sa.Column("stage", sa.String(), nullable=True),
        sa.Column("message", sa.String(), nullable=True),
    )


def downgrade():
    op.drop_table("assessment_events")
