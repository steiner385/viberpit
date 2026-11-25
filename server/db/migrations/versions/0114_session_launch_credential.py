"""Remote Control on runners: sessions.launch_credential.

Revision ID: 0114_session_launch_credential
Revises: 0113_session_remote_control

What a session's CLI last launched with (slot | setup | envelope | cfg | copilot), written
by the host that runs it, so the primary's mirror of a runner session can tell a Remote
Control launch (an access token only) from one that owns the slot's refresh family.
Nullable: ``init_db``'s reconcile adds it on both dialects; this revision is the reviewable
record.
"""
import sqlalchemy as sa
from alembic import op

revision = "0114_session_launch_credential"
down_revision = "0113_session_remote_control"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("launch_credential", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "launch_credential")
