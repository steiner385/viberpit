"""fleet deploy fan-out — deploy_state table + hosts.checkout_sha

Revision ID: 0017_deploy_state
Revises: 0016_host_heartbeats
Create Date: 2026-09-03

Reviewable record only: nothing runs `alembic upgrade` at deploy time — init_db's
create_all builds the new table and _reconcile_columns ALTER-ADDs the nullable column
on both SQLite and Postgres at next boot.
"""
from alembic import op
import sqlalchemy as sa

revision = "0017_deploy_state"
down_revision = "0016_host_heartbeats"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deploy_state",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("deployed_sha", sa.String(), nullable=True),
        sa.Column("deployed_short", sa.String(), nullable=True),
        sa.Column("daemon_sha", sa.String(), nullable=True),
        sa.Column("deployed_at", sa.DateTime(), nullable=True),
        sa.Column("deployed_by", sa.String(), nullable=True),
    )
    op.add_column("hosts", sa.Column("checkout_sha", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("hosts", "checkout_sha")
    op.drop_table("deploy_state")
