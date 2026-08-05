"""estimated run-rate spend flag on assessments

Revision ID: 010
Revises: 009
Create Date: 2026-08-06
"""
from alembic import op
import sqlalchemy as sa

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("assessments") as batch:
        batch.add_column(sa.Column("spend_estimated", sa.Integer(), server_default="0"))
        batch.add_column(sa.Column("spend_period_days", sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table("assessments") as batch:
        batch.drop_column("spend_period_days")
        batch.drop_column("spend_estimated")
