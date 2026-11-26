"""app_settings.remote_machine_labels: the operator's names for Remote Control machine ids.

Revision ID: 0116_remote_machine_labels
Revises: 0115_display_target

A JSON object ``{machine_id: label}`` naming a machine ViberPit does not run on, shown on
the remote sessions it hosts (``server/remote_sessions/machines.py``). Nullable: ``init_db``'s
reconcile adds it on both dialects; this revision is the reviewable record.
"""
import sqlalchemy as sa
from alembic import op

revision = "0116_remote_machine_labels"
down_revision = "0115_display_target"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("app_settings",
                  sa.Column("remote_machine_labels", sa.JSON(none_as_null=True), nullable=True))


def downgrade() -> None:
    op.drop_column("app_settings", "remote_machine_labels")
