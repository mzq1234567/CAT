"""assessment progress + state machine + tenant isolation columns

Revision ID: 002
Revises: 001
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("assessments") as batch:
        batch.add_column(sa.Column("tenant_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("progress", sa.Integer(), server_default="0"))
        batch.add_column(sa.Column("status_message", sa.String(), nullable=True))
        batch.add_column(sa.Column("needs_review_count", sa.Integer(), server_default="0"))
        batch.add_column(sa.Column("snapshot_at", sa.DateTime(), nullable=True))
        batch.create_index("ix_assessments_tenant_id", ["tenant_id"])


def downgrade():
    with op.batch_alter_table("assessments") as batch:
        batch.drop_index("ix_assessments_tenant_id")
        batch.drop_column("snapshot_at")
        batch.drop_column("needs_review_count")
        batch.drop_column("status_message")
        batch.drop_column("progress")
        batch.drop_column("tenant_id")
