"""app_sessions.scope / token_id: the read-only kiosk (display) principal (#1408).

Revision ID: 0117_display_scope
Revises: 0116_remote_machine_labels

``scope`` NULL means "full"; ``"display"`` marks a session minted by the kiosk exchange.
``token_id`` names the ``assistant_tokens`` row whose revocation/expiry ends it. Both
nullable: ``init_db``'s reconcile adds them on both dialects; this revision is the record.
"""
import sqlalchemy as sa
from alembic import op

revision = "0117_display_scope"
down_revision = "0116_remote_machine_labels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("app_sessions", sa.Column("scope", sa.String(), nullable=True))
    op.add_column("app_sessions", sa.Column("token_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("app_sessions", "token_id")
    op.drop_column("app_sessions", "scope")
