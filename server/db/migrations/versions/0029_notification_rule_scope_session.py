"""Assistant watches (#1354): NotificationRule.scope_session_id, a per-session scope
column for kind="session" watches ("Tell me when this session finishes").

Revision ID: 0029_notification_rule_scope_session
Revises: 0028_assistant_tokens
Create Date: 2026-09-06

Reviewable record only: nothing runs `alembic upgrade` at deploy time — init_db's
reconcile ALTER-ADDs the nullable column on both SQLite and Postgres at next boot.
"""
from alembic import op
import sqlalchemy as sa

revision = "0029_notification_rule_scope_session"
down_revision = "0028_assistant_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "notification_rule",
        sa.Column("scope_session_id", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("notification_rule", "scope_session_id")
