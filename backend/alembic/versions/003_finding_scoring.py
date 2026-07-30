"""finding confidence, advisor correlation, validation + debug_reason columns

Revision ID: 003
Revises: 002
Create Date: 2026-07-23
"""
from alembic import op
import sqlalchemy as sa

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("findings") as batch:
        batch.add_column(sa.Column("confidence", sa.Float(), server_default="0"))
        batch.add_column(sa.Column("advisor_recommendation_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("validation_status", sa.String(), nullable=True))
        batch.add_column(sa.Column("validation_variance_pct", sa.Float(), nullable=True))
        batch.add_column(sa.Column("actual_monthly_cost", sa.Float(), nullable=True))
        batch.add_column(sa.Column("debug_reason", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("findings") as batch:
        batch.drop_column("debug_reason")
        batch.drop_column("actual_monthly_cost")
        batch.drop_column("validation_variance_pct")
        batch.drop_column("validation_status")
        batch.drop_column("advisor_recommendation_id")
        batch.drop_column("confidence")
