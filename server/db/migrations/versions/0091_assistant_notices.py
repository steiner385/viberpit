"""Durable assistant watch notices (#3183): the assistant_notices table.

Revision ID: 0091_assistant_notices
Revises: 0090_work_item_comment_owner_ids

A fired watch used to reach the drawer only as a live event or an in-memory queue,
both lost when no client was attached or the daemon restarted. The row is now the
delivery. ``create_all`` makes the table on both dialects; this revision is the
reviewable record.
"""
import sqlalchemy as sa
from alembic import op

revision = "0091_assistant_notices"
down_revision = "0090_work_item_comment_owner_ids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assistant_notices",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("identity", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("url", sa.String(), nullable=True),
        sa.Column("watch_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("read_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_assistant_notices_identity", "assistant_notices", ["identity"])


def downgrade() -> None:
    op.drop_index("ix_assistant_notices_identity", table_name="assistant_notices")
    op.drop_table("assistant_notices")
