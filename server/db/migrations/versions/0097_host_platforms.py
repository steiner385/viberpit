"""Host platforms: hosts.platforms and sessions.platform.

Revision ID: 0097_host_platforms
Revises: 0096_session_views

``hosts.platforms`` is the last-seen ``/info.platforms`` list (NULL = Linux only);
``sessions.platform`` is the platform a session must run on (NULL = no requirement).
Both nullable, so init_db reconcile adds them on both dialects; this revision is the
reviewable record.
"""
import sqlalchemy as sa
from alembic import op

revision = "0097_host_platforms"
down_revision = "0096_session_views"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hosts", sa.Column("platforms", sa.JSON(), nullable=True))
    op.add_column("sessions", sa.Column("platform", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "platform")
    op.drop_column("hosts", "platforms")
