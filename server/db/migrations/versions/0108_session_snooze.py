"""Session snooze: ``sessions.snooze`` (nullable JSON).

Revision ID: 0108_session_snooze
Revises: 0107_milestone_notes

``sessions.snooze`` holds ``{at, until, reason, by}`` while a session is snoozed and
NULL otherwise (docs/superpowers/specs/2026-11-06-session-snooze-design.md). Nullable,
so ``init_db``'s reconcile ALTER-ADDs it on both dialects; this revision is the
reviewable record.

Chaining note: chained off 0107_milestone_notes (master's newest revision when written).
If another revision lands first, re-chain ``down_revision`` to the newest one
(``test_alembic_single_head`` enforces one head).
"""
import sqlalchemy as sa
from alembic import op

revision = "0108_session_snooze"
down_revision = "0107_milestone_notes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("snooze", sa.JSON(none_as_null=True), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "snooze")
