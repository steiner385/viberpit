"""``sessions.pr_resolution_note`` (spec 2026-09-18 §5).

Revision ID: 0052_pr_resolution_note
Revises: 0051_pr_resolution_source_observed

The one-sentence ``note`` argument of ``mark_no_pr`` — the session's own account of
why it finished without a PR. It was parsed and then discarded; nothing recorded it.
The Backlog Runner needs it to settle an ended INVESTIGATION (series item 4b): the
convert-to-a-fix exit is recognised by the note's prefix, and the escalate exit's
note becomes the reason a human is handed the issue.

Nullable, no DB-level default: NULL on every pre-existing row (and on every
resolution that arrived by marker or classifier, neither of which carries a note).

Nothing runs ``alembic upgrade`` at deploy time: ``init_db``'s reconcile ALTER-ADDs
the nullable column on both dialects. This revision is the reviewable record.
"""
from alembic import op
import sqlalchemy as sa

revision = "0052_pr_resolution_note"
down_revision = "0051_pr_resolution_source_observed"
branch_labels = None
depends_on = None


def _existing() -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {c["name"] for c in inspector.get_columns("sessions")}


def upgrade() -> None:
    if "pr_resolution_note" not in _existing():
        op.add_column("sessions", sa.Column("pr_resolution_note", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "pr_resolution_note")
