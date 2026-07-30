"""actual spend (current cost + per-area breakdown) on assessments

Revision ID: 005
Revises: 004
Create Date: 2026-07-28
"""
from alembic import op
import sqlalchemy as sa

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("assessments") as batch:
        batch.add_column(sa.Column("current_monthly_spend", sa.Float(), nullable=True))
        batch.add_column(sa.Column("current_annual_spend", sa.Float(), nullable=True))
        batch.add_column(sa.Column("spend_by_area", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("cost_data_available", sa.Integer(), server_default="0"))


def downgrade():
    with op.batch_alter_table("assessments") as batch:
        batch.drop_column("cost_data_available")
        batch.drop_column("spend_by_area")
        batch.drop_column("current_annual_spend")
        batch.drop_column("current_monthly_spend")
