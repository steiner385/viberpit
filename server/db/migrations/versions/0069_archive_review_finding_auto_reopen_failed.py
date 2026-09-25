"""Archive review finding auto_reopen_failed flag (#1906).

Revision ID: 0069_archive_review_finding_auto_reopen_failed
Revises: 0068_artifact_frontmatter

``archive_review_finding.auto_reopen_failed`` (nullable bool) marks a finding whose
``auto_reopen`` attempt actually failed (e.g. ``ArchiveMissingError`` on a legacy row
whose archive directory moved or was deleted). Previously the finding was marked
``reopened`` — a durable, never-resurrected decision — the moment ``auto_reopen`` mode
picked it, regardless of whether the underlying unarchive succeeded. A failing session
was then silently reported as resolved while the daemon retried and logged the same
WARNING every tick for as long as the session stayed inside the re-audit window
(#1906). The finding is now reverted to ``open`` with this flag set so a human sees it
via the review-finding action provider instead of it looping forever in the logs.

Nothing runs ``alembic upgrade`` at deploy time: ``init_db``'s ``_reconcile_columns``
ALTER-ADDs the nullable column on both dialects. This revision is the reviewable
record of that change.
"""
from alembic import op
import sqlalchemy as sa

revision = "0069_archive_review_finding_auto_reopen_failed"
down_revision = "0068_artifact_frontmatter"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("archive_review_finding",
                  sa.Column("auto_reopen_failed", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("archive_review_finding", "auto_reopen_failed")
