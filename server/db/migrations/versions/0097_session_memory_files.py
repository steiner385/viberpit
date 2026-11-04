"""The CLI's loaded memory files per session (D-13): sessions.memory_files.

Revision ID: 0097_session_memory_files
Revises: 0096_session_views

``[{path, type, tokens}]`` in CLI load order, from the last context reading. Replayed on
attach with the context usage, and the allowlist of the owning-host memory-file viewer.
Nullable: ``init_db``'s reconcile adds it on both dialects; this revision is the
reviewable record.
"""
import sqlalchemy as sa
from alembic import op

revision = "0097_session_memory_files"
down_revision = "0096_session_views"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("memory_files", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "memory_files")
