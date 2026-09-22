"""Flag a backlog grouping generation the parser fell back to (all singletons).

Revision ID: 0060_backlog_groups_degraded
Revises: 0059_project_unindexed_docs

Nullable BOOLEAN: init_db reconcile adds this on SQLite and Postgres. Rows of
earlier generations stay NULL (read as "not degraded"); the next plan refresh
rewrites the table wholesale and stamps every row of its generation (#2217).
"""
from alembic import op
import sqlalchemy as sa

revision = "0060_backlog_groups_degraded"
down_revision = "0059_project_unindexed_docs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("backlog_groups")}
    if "degraded" not in columns:
        op.add_column("backlog_groups", sa.Column("degraded", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("backlog_groups", "degraded")
