"""Per-session PR auto-fix opt-out (D-14): sessions.pr_autofix_paused.

Revision ID: 0100_session_pr_autofix_paused
Revises: 0099_session_fast_mode

NULL/False = PR Doctor may fix the session's PRs; True = ``_pr_doctor_step`` skips them
(notifications still fire). Independent of ``autopilot_paused``. Nullable: ``init_db``'s
reconcile adds it on both dialects; this revision is the reviewable record.
"""
import sqlalchemy as sa
from alembic import op

revision = "0100_session_pr_autofix_paused"
down_revision = "0099_session_fast_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("pr_autofix_paused", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "pr_autofix_paused")
