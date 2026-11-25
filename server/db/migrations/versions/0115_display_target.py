"""Per-host display target: hosts.display_target/display_ssh_host/display_bridge, sessions.display_target_host.

Revision ID: 0115_display_target
Revises: 0114_session_launch_credential

``display_target`` (``local`` | ``host:<name>``; NULL = local) is where a host's sessions open
GUI windows; ``display_ssh_host`` is the ssh destination ON the source host (as its session
user) that the target's bridge logs into (falls back to ``ssh_host``); ``display_bridge`` is the probe snapshot of the host's bridge; and
``sessions.display_target_host`` records the target a remote-display session was spawned for.
All nullable: ``init_db``'s reconcile adds them on both dialects; this revision is the
reviewable record.
"""
import sqlalchemy as sa
from alembic import op

revision = "0115_display_target"
down_revision = "0114_session_launch_credential"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hosts", sa.Column("display_target", sa.String(), nullable=True))
    op.add_column("hosts", sa.Column("display_ssh_host", sa.String(), nullable=True))
    op.add_column("hosts", sa.Column("display_bridge", sa.JSON(none_as_null=True), nullable=True))
    op.add_column("sessions", sa.Column("display_target_host", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "display_target_host")
    op.drop_column("hosts", "display_bridge")
    op.drop_column("hosts", "display_ssh_host")
    op.drop_column("hosts", "display_target")
