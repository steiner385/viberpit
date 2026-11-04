"""Per-session fast mode (D-14): sessions.fast_mode.

Revision ID: 0099_session_fast_mode
Revises: 0098_session_memory_files

The session's fast-mode choice, re-applied through the spawn ``--settings`` JSON on
every resume and respawn. NULL is off (the default). Nullable: ``init_db``'s reconcile
adds it on both dialects; this revision is the reviewable record.
"""
import sqlalchemy as sa
from alembic import op

revision = "0099_session_fast_mode"
down_revision = "0098_session_memory_files"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("fast_mode", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "fast_mode")
