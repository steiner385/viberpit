"""Persist pm-sync's per-pass constitution governance status onto the project row.

Revision ID: 0062_project_constitution_status
Revises: 0061_brand_automation_keys

Nullable string: init_db reconcile adds this on SQLite and Postgres. Existing
rows stay NULL until the next pm-sync pass classifies them.
"""
from alembic import op
import sqlalchemy as sa

revision = "0062_project_constitution_status"
down_revision = "0061_brand_automation_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("projects")}
    if "constitution_status" not in columns:
        op.add_column("projects", sa.Column("constitution_status", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "constitution_status")
