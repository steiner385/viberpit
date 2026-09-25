"""sessions.suspected_complete_reason — the suspected-complete overlay on a
handed-back session (companion to PR #2473 / issue #2474 discussion, 2026-09-25).

Revision ID: 0071_session_suspected_complete_reason
Revises: 0070_persona

Nothing runs `alembic upgrade` at deploy time: the column is nullable, so
`_reconcile_columns` ALTER-ADDs it on both dialects at startup. This revision is
the reviewable record of that change.
"""
from alembic import op
import sqlalchemy as sa

revision = "0071_session_suspected_complete_reason"
down_revision = "0070_persona"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("sessions")}
    if "suspected_complete_reason" not in cols:
        op.add_column("sessions", sa.Column("suspected_complete_reason", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "suspected_complete_reason")
