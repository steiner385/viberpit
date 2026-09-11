"""child sessions: parent_session_id, parent_notified_status on sessions

Revision ID: 0038_child_sessions
Revises: 0037_triage_undo_snapshot

Reviewable record of the schema change (spec 2026-09-11-child-session-dispatch §4.1).
Nothing runs `alembic upgrade` at deploy time: init_db's reconcile adds both nullable
columns on SQLite and Postgres.
"""
from alembic import op
import sqlalchemy as sa

revision = "0038_child_sessions"
down_revision = "0037_triage_undo_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("parent_session_id", sa.String(), nullable=True))
    op.add_column("sessions", sa.Column("parent_notified_status", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "parent_notified_status")
    op.drop_column("sessions", "parent_session_id")
