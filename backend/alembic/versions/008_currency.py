"""billing currency on assessments

Revision ID: 008
Revises: 007
Create Date: 2026-07-30
"""
from alembic import op
import sqlalchemy as sa

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("assessments") as batch:
        batch.add_column(sa.Column("currency", sa.String(), nullable=True, server_default="USD"))


def downgrade():
    with op.batch_alter_table("assessments") as batch:
        batch.drop_column("currency")
