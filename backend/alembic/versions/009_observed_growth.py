"""observed annual spend-growth rate on assessments

Revision ID: 009
Revises: 008
Create Date: 2026-07-31
"""
from alembic import op
import sqlalchemy as sa

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("assessments") as batch:
        batch.add_column(sa.Column("observed_annual_growth", sa.Float(), nullable=True))


def downgrade():
    with op.batch_alter_table("assessments") as batch:
        batch.drop_column("observed_annual_growth")
