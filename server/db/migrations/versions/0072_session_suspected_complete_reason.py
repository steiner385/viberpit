"""sessions.suspected_complete_reason — the suspected-complete overlay on a
handed-back session (companion to PR #2473 / issue #2474 discussion, 2026-09-25).

Revision ID: 0072_session_suspected_complete_reason
Revises: 0071_error_signature_repo

Nothing runs `alembic upgrade` at deploy time: the column is nullable, so
`_reconcile_columns` ALTER-ADDs it on both dialects at startup. This revision is
the reviewable record of that change.
"""
from alembic import op
import sqlalchemy as sa

revision = "0072_session_suspected_complete_reason"
down_revision = "0071_error_signature_repo"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("sessions")}
    if "suspected_complete_reason" not in cols:
        op.add_column("sessions", sa.Column("suspected_complete_reason", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "suspected_complete_reason")
