"""AI assistant: sessions.kind / owner_identity, app_settings.assistant_*, audit_events.via.

Record only — `_reconcile_columns` ALTER-ADDs these nullable columns on both dialects at
startup. The index is NOT created by reconcile (it never creates indexes on existing
tables); create it by hand on the live DB:

    CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_sessions_kind_owner
        ON sessions (kind, owner_identity);
"""
import sqlalchemy as sa
from alembic import op

revision = "0022_assistant_sessions"
down_revision = "0021_work_item_assignees"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("kind", sa.String(), nullable=True))
    op.add_column("sessions", sa.Column("owner_identity", sa.String(), nullable=True))
    op.create_index("ix_sessions_kind_owner", "sessions", ["kind", "owner_identity"])
    op.add_column("app_settings", sa.Column("assistant_enabled", sa.Boolean(), nullable=True))
    op.add_column("app_settings", sa.Column("assistant_model", sa.String(), nullable=True))
    op.add_column("audit_events", sa.Column("via", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("audit_events", "via")
    op.drop_column("app_settings", "assistant_model")
    op.drop_column("app_settings", "assistant_enabled")
    op.drop_index("ix_sessions_kind_owner", table_name="sessions")
    op.drop_column("sessions", "owner_identity")
    op.drop_column("sessions", "kind")
