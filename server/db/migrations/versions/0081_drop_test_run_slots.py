"""Retire the DB-backed test-run slot table (#2887); the host-local flock slot supersedes it.

Revision ID: 0081_drop_test_run_slots
Revises: 0080_session_display_fallback_reason

Note: nothing runs ``alembic upgrade`` at rollout time, so a live DB keeps the
now-unmapped table as an inert orphan until someone drops it by hand; nothing reads it.
"""
import sqlalchemy as sa
from alembic import op

revision = "0081_drop_test_run_slots"
down_revision = "0080_session_display_fallback_reason"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("test_run_slots")


def downgrade() -> None:
    op.create_table(
        "test_run_slots",
        sa.Column("session_id", sa.String(), primary_key=True),
        sa.Column("host", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
    )
