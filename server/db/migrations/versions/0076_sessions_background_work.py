"""sessions.background_work: the open background-work snapshot behind a "Waiting…" card (#2635).

Record only — the column is nullable, so `_reconcile_columns` ALTER-ADDs it on both
dialects at startup; nothing runs `alembic upgrade` at deploy time. NULL means nothing
open.
"""
import sqlalchemy as sa
from alembic import op

revision = "0076_sessions_background_work"
down_revision = "0075_quiet_hours_timezone"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("background_work", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "background_work")
