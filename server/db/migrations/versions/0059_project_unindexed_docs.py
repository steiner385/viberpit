"""Persist pm-sync's per-pass unparseable feature docs onto the project row.

Revision ID: 0059_project_unindexed_docs
Revises: 0058_project_former_slugs

Nullable JSON: init_db reconcile adds this on SQLite and Postgres. Existing
rows stay NULL (nothing skipped) until the next pm-sync pass fills it in.
"""
from alembic import op
import sqlalchemy as sa

revision = "0059_project_unindexed_docs"
down_revision = "0058_project_former_slugs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("projects")}
    if "unindexed_docs" not in columns:
        op.add_column("projects", sa.Column("unindexed_docs", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "unindexed_docs")
