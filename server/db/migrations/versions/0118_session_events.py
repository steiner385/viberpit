"""session_events timeline + sessions.last_spawn_failure (#947, spec 002 Phase 2).

Revision ID: 0118_session_events
Revises: 0117_display_scope

``session_events`` is a new table (``create_all`` installs it whole); the nullable
``sessions.last_spawn_failure`` column is ALTER-added by ``init_db``'s reconcile on both
dialects. This revision is the reviewable record.
"""
import sqlalchemy as sa
from alembic import op

revision = "0118_session_events"
down_revision = "0117_display_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_events",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
    )
    op.create_index("ix_session_events_session_ts", "session_events", ["session_id", "ts"])
    op.add_column("sessions", sa.Column("last_spawn_failure", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "last_spawn_failure")
    op.drop_index("ix_session_events_session_ts", table_name="session_events")
    op.drop_table("session_events")
