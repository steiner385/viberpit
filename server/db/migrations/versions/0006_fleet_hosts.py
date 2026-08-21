"""add hosts registry table and sessions.host

Revision ID: 0006_fleet_hosts
Revises: 0005_session_archive_root
Create Date: 2026-08-21

Adds the ``hosts`` registry and one nullable column (``sessions.host``) recording
which fleet host executed a session. ``create_all`` makes absent tables and
``_reconcile_columns`` ALTER-ADDs nullable columns on every dialect (#717), so this
revision is the reviewable record rather than the only application path.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_fleet_hosts"
down_revision: Union[str, None] = "0005_session_archive_root"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "hosts",
        sa.Column("name", sa.String(), primary_key=True),
        sa.Column("endpoint", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("weight", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("max_sessions", sa.Integer(), nullable=True),
        sa.Column("healthy", sa.Boolean(), nullable=True),
        sa.Column("last_probe_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("sha", sa.String(), nullable=True),
        sa.Column("protocol", sa.Integer(), nullable=True),
        sa.Column("load_avg", sa.Float(), nullable=True),
        sa.Column("mem_available_mb", sa.Integer(), nullable=True),
        sa.Column("active_sessions", sa.Integer(), nullable=True),
    )
    op.add_column("sessions", sa.Column("host", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "host")
    op.drop_table("hosts")
