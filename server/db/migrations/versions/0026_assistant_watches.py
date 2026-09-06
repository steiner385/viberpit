"""notification_rule.once / owner_session_id / expires_at for assistant watches.

Record only — `_reconcile_columns` ALTER-ADDs these nullable columns on both
dialects at startup (same docstring pattern as 0022_assistant_sessions). A
plain #602 routing rule leaves all three NULL; an assistant watch (#1210 Pack N,
Task 8.3) sets them to disable itself after firing once, scope listing/deletion
to the owning assistant session, and expire after its TTL.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026_assistant_watches"
down_revision = "0025_host_engines"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("notification_rule", sa.Column("once", sa.Boolean(), nullable=True))
    op.add_column("notification_rule", sa.Column("owner_session_id", sa.String(), nullable=True))
    op.add_column(
        "notification_rule", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("notification_rule", "expires_at")
    op.drop_column("notification_rule", "owner_session_id")
    op.drop_column("notification_rule", "once")
