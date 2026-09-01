"""fleet host upgrade config — hosts.ssh_host + hosts.repo_path

Revision ID: 0013_host_upgrade_ssh_config
Revises: 0012_model_approval_columns
Create Date: 2026-09-01

Reviewable record only: nothing runs `alembic upgrade` at deploy time — init_db's
_reconcile_columns ALTER-ADDs these nullable columns on both SQLite and Postgres
at next boot. Both columns are nullable (NULL = in-app upgrade not configured for
that host), so no default-synthesis / drift issue.
"""
from alembic import op
import sqlalchemy as sa

revision = "0013_host_upgrade_ssh_config"
down_revision = "0012_model_approval_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hosts", sa.Column("ssh_host", sa.String(), nullable=True))
    op.add_column("hosts", sa.Column("repo_path", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("hosts", "repo_path")
    op.drop_column("hosts", "ssh_host")
