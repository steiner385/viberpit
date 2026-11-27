"""sessions.auto_resume_opt_out — per-session veto of restart auto-resume (spec 002 FR-306).

Revision ID: 0120_auto_resume_opt_out
Revises: 0119_session_holds

Nullable boolean; ``init_db`` reconcile applies it, this revision is the reviewable record.
"""
import sqlalchemy as sa
from alembic import op

revision = "0120_auto_resume_opt_out"
down_revision = "0119_session_holds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("auto_resume_opt_out", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "auto_resume_opt_out")
