"""Archive Audit review findings (Phase 4, #1611).

Revision ID: 0067_archive_review_finding
Revises: 0066_feature_evidence

Archive Audit's default effect changes from automatic reopening to a durable
review finding: ``archive_review_finding`` records ONE row per session that
re-classifies premature, deduped by ``session_id``, so a human decision
(retain/reopen) is never silently resurrected by a later tick. Automatic
reopening survives as the explicit ``auto_reopen`` mode.

Nothing runs ``alembic upgrade`` at deploy time: ``create_all`` creates this
new table on both dialects. This revision is the reviewable record of that
change.
"""
from alembic import op
import sqlalchemy as sa

revision = "0067_archive_review_finding"
down_revision = "0066_feature_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "archive_review_finding",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("repo", sa.String(), nullable=True),
        sa.Column("goal", sa.Text(), nullable=True),
        sa.Column("bucket", sa.String(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("decided_by", sa.String(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("session_id", name="uq_archive_review_finding_session"),
    )
    op.create_index("ix_archive_review_finding_session_id", "archive_review_finding",
                    ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_archive_review_finding_session_id", table_name="archive_review_finding")
    op.drop_table("archive_review_finding")
