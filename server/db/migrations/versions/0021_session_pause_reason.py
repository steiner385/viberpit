"""Persisted pause reason + earliest reset for usage-stalled sessions.

Spec: docs/superpowers/specs/2026-09-04-usage-stalled-rotation-design.md. Both columns
are nullable so ``_reconcile_columns`` applies them at startup on SQLite and Postgres;
this revision is the reviewable record.
"""
import sqlalchemy as sa
from alembic import op

revision = "0021_session_pause_reason"
down_revision = "0020_journal_mutation_id_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("paused_reason", sa.String(), nullable=True))
    op.add_column("sessions", sa.Column("paused_until", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "paused_until")
    op.drop_column("sessions", "paused_reason")
