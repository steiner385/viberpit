"""D-22 branch ownership registry: session_branches.

Revision ID: 0109_session_branches
Revises: 0108_session_snooze

One row per branch ViberPit created (``source``: create | fork | import | backfill),
unique per ``(host, clone, branch)`` because clones are per host while the DB is
shared. ``session_id`` is indexed but carries no FK: the record must outlive a
hard-deleted session row while the branch still exists. ``init_db``'s ``create_all``
creates the table on both dialects (and its one-shot backfill seeds it); this revision
is the reviewable record.

Chaining note: chained off 0108_session_snooze (origin/master's newest revision when
written; re-chained from 0107 when 0108_session_snooze landed first). If another revision lands first, re-chain ``down_revision`` to the newest one
(``test_alembic_single_head`` enforces one head).
"""
import sqlalchemy as sa
from alembic import op

revision = "0109_session_branches"
down_revision = "0108_session_snooze"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_branches",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("host", sa.String(), nullable=False),
        sa.Column("clone", sa.String(), nullable=False),
        sa.Column("branch", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("host", "clone", "branch",
                            name="uq_session_branches_host_clone_branch"),
    )
    op.create_index("ix_session_branches_session_id", "session_branches", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_session_branches_session_id", table_name="session_branches")
    op.drop_table("session_branches")
