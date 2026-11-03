"""Fork from here (D-8): sessions.fork_source_engine_session_id + fork_resume_at.

Revision ID: 0093_session_fork_resume
Revises: 0092_archive_finding_awaiting_edge

A fork child's pending fork parameters, so a daemon restart before its first turn
still resumes the source as a fork. Nullable, so ``init_db``'s reconcile ALTER-ADDs
them on both dialects; this revision is the reviewable record.
"""
import sqlalchemy as sa
from alembic import op

revision = "0093_session_fork_resume"
down_revision = "0092_archive_finding_awaiting_edge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("fork_source_engine_session_id", sa.String(),
                                        nullable=True))
    op.add_column("sessions", sa.Column("fork_resume_at", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "fork_resume_at")
    op.drop_column("sessions", "fork_source_engine_session_id")
