"""full-inventory totals (resources scanned + distinct types) on assessments

Revision ID: 006
Revises: 005
Create Date: 2026-07-28
"""
from alembic import op
import sqlalchemy as sa

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("assessments") as batch:
        batch.add_column(sa.Column("total_resources", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("resource_type_count", sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table("assessments") as batch:
        batch.drop_column("resource_type_count")
        batch.drop_column("total_resources")
