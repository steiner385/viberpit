"""Session hand-back reason (#2321).

Revision ID: 0064_session_hand_back_reason
Revises: 0063_artifact_upstream_ref

``sessions.hand_back_reason`` (nullable string) stores the one sentence a session
passed to ``hand_back(reason)``. Until now that sentence existed only in an audit
row and a log line, so a handed-back card could say that something was wanted but
not what — the user had to open the session and read the transcript to find out.

Written by ``_persist_status`` together with the ``handed_back`` status and cleared
on every other status, so the column always describes the CURRENT hand-back.

Nothing runs ``alembic upgrade`` at deploy time: ``init_db``'s
``_reconcile_columns`` ALTER-ADDs the nullable column on both dialects. This
revision is the reviewable record of that change.
"""
from alembic import op
import sqlalchemy as sa

revision = "0064_session_hand_back_reason"
down_revision = "0063_artifact_upstream_ref"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions",
                  sa.Column("hand_back_reason", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "hand_back_reason")
