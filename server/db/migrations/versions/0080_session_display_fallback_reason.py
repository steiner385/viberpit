"""Native display routing: sessions.display_fallback_reason (why a local session went virtual).

Revision ID: 0080_session_display_fallback_reason
Revises: 0079_mcp_repo_scope
"""
import sqlalchemy as sa
from alembic import op

revision = "0080_session_display_fallback_reason"
down_revision = "0079_mcp_repo_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("display_fallback_reason", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "display_fallback_reason")
