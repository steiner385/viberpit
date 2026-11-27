"""session_holds — durable pending asks (#947, spec 002 FR-305).

Revision ID: 0119_session_holds
Revises: 0118_session_events

A new table (``create_all`` installs it whole on both dialects); this revision is the
reviewable record. Written by ``server/sessions/holds.py`` only.
"""
import sqlalchemy as sa
from alembic import op

revision = "0119_session_holds"
down_revision = "0118_session_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_holds",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("content", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_session_holds_session_created", "session_holds",
                    ["session_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_session_holds_session_created", table_name="session_holds")
    op.drop_table("session_holds")
