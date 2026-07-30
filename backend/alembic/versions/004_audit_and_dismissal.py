"""audit_logs table + finding dismissal columns

Revision ID: 004
Revises: 003
Create Date: 2026-07-23
"""
from alembic import op
import sqlalchemy as sa

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("timestamp", sa.DateTime()),
        sa.Column("event", sa.String()),
        sa.Column("user_id", sa.String()),
        sa.Column("tenant_id", sa.String(), nullable=True),
        sa.Column("user_email", sa.String(), nullable=True),
        sa.Column("resource", sa.String(), nullable=True),
        sa.Column("request_id", sa.String(), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
    )
    op.create_index("ix_audit_logs_event", "audit_logs", ["event"])
    op.create_index("ix_audit_logs_user_id", "audit_logs", ["user_id"])
    op.create_index("ix_audit_logs_tenant_id", "audit_logs", ["tenant_id"])
    op.create_index("ix_audit_logs_timestamp", "audit_logs", ["timestamp"])

    with op.batch_alter_table("findings") as batch:
        batch.add_column(sa.Column("dismissed", sa.Integer(), server_default="0"))
        batch.add_column(sa.Column("dismissed_by", sa.String(), nullable=True))
        batch.add_column(sa.Column("dismissed_at", sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("findings") as batch:
        batch.drop_column("dismissed_at")
        batch.drop_column("dismissed_by")
        batch.drop_column("dismissed")
    op.drop_table("audit_logs")
