"""D-23 sandbox for Claude sessions: sessions.sandbox / sandbox_state / sandbox_reason and
app_settings.sandbox_default.

Revision ID: 0111_session_sandbox
Revises: 0110_app_settings_branch_prefix

``sessions.sandbox`` is the session's setting, recorded at its first spawn (NULL = a row
spawned before D-23, read as off); ``sandbox_state`` / ``sandbox_reason`` are the last
verification of its CLI. ``app_settings.sandbox_default`` decides new sessions (NULL =
off). All nullable: ``init_db``'s reconcile adds them on both dialects; this revision is
the reviewable record.

Chaining note: chained off 0110_app_settings_branch_prefix (the newest revision when
written). If another revision lands first, re-chain ``down_revision`` to the newest one
(``test_alembic_single_head`` enforces one head).
"""
import sqlalchemy as sa
from alembic import op

revision = "0111_session_sandbox"
down_revision = "0110_app_settings_branch_prefix"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("sandbox", sa.Boolean(), nullable=True))
    op.add_column("sessions", sa.Column("sandbox_state", sa.String(), nullable=True))
    op.add_column("sessions", sa.Column("sandbox_reason", sa.String(), nullable=True))
    op.add_column("app_settings", sa.Column("sandbox_default", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("app_settings", "sandbox_default")
    op.drop_column("sessions", "sandbox_reason")
    op.drop_column("sessions", "sandbox_state")
    op.drop_column("sessions", "sandbox")
