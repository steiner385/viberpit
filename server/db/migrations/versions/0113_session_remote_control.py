"""Remote Control: sessions.remote_control, sessions.remote_control_url,
app_settings.remote_control_default.

Revision ID: 0113_session_remote_control
Revises: 0112_product_repo_lifecycle

A session's Remote Control override (NULL = follow the app default), the claude.ai
URL its last enable returned, and the app-wide default (NULL = on). All nullable:
``init_db``'s reconcile adds them on both dialects; this revision is the reviewable
record.
"""
import sqlalchemy as sa
from alembic import op

revision = "0113_session_remote_control"
down_revision = "0112_product_repo_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("remote_control", sa.Boolean(), nullable=True))
    op.add_column("sessions", sa.Column("remote_control_url", sa.String(), nullable=True))
    op.add_column("app_settings",
                  sa.Column("remote_control_default", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("app_settings", "remote_control_default")
    op.drop_column("sessions", "remote_control_url")
    op.drop_column("sessions", "remote_control")
