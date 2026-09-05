"""AI assistant v2: app_settings.assistant_operator_tools (operator tier, spec v2 B1).

Record only — the column is nullable, so `_reconcile_columns` ALTER-ADDs it on both
dialects at startup; nothing runs `alembic upgrade` at deploy time. NULL means "unset →
fall through to CCSWITCH_ASSISTANT_OPERATOR_TOOLS", which defaults false.

Chained off `0026_assistant_watches` (PR #1259), which was opened first and claims 0026
while this branch was in flight. Both would otherwise chain off 0025 and fork the chain
into two heads — harmless at deploy time, since reconcile and not alembic applies the
schema, but the revisions are meant to read as a linear record of what changed.
"""
import sqlalchemy as sa
from alembic import op

revision = "0027_assistant_operator_tools"
down_revision = "0026_assistant_watches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("app_settings", sa.Column("assistant_operator_tools", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("app_settings", "assistant_operator_tools")
