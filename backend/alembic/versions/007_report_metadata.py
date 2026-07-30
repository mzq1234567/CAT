"""report metadata (tenant/subscription display names + major resource types) on assessments

Revision ID: 007
Revises: 006
Create Date: 2026-07-29
"""
from alembic import op
import sqlalchemy as sa

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("assessments") as batch:
        batch.add_column(sa.Column("tenant_display_name", sa.String(), nullable=True))
        batch.add_column(sa.Column("subscription_names", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("major_resource_types", sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table("assessments") as batch:
        batch.drop_column("major_resource_types")
        batch.drop_column("subscription_names")
        batch.drop_column("tenant_display_name")
