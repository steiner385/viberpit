"""add automation registry tables and session provenance columns

Revision ID: 0002_automation_registry
Revises: 0001_create_attachments
Create Date: 2026-06-30

Adds three tables (automation, automation_event, automation_target_state) and
two nullable provenance columns (origin, origin_ref) to the sessions table.
SQLite dev relies on init_db()/create_all + _reconcile_sqlite_columns; this
migration is the Postgres/prod path for schema changes introduced by the
automation shell feature.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_automation_registry"
down_revision: Union[str, None] = "0001_create_attachments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- automation table ---
    op.create_table(
        "automation",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("mode", sa.String(), nullable=True),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("last_active_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    # --- automation_event table ---
    op.create_table(
        "automation_event",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("automation_id", sa.String(), nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("level", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=True),
        sa.Column("summary", sa.String(), nullable=False),
        sa.Column("target", sa.String(), nullable=True),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["automation_id"], ["automation.id"]),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_automation_event_automation_id", "automation_event", ["automation_id"])
    op.create_index("ix_automation_event_ts", "automation_event", ["ts"])

    # --- automation_target_state table ---
    op.create_table(
        "automation_target_state",
        sa.Column("automation_id", sa.String(), nullable=False),
        sa.Column("target", sa.String(), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["automation_id"], ["automation.id"]),
        sa.PrimaryKeyConstraint("automation_id", "target"),
    )

    # --- session provenance columns ---
    op.add_column("sessions", sa.Column("origin", sa.String(), nullable=True))
    op.add_column("sessions", sa.Column("origin_ref", sa.String(), nullable=True))


def downgrade() -> None:
    # remove session provenance columns
    op.drop_column("sessions", "origin_ref")
    op.drop_column("sessions", "origin")

    # drop tables in reverse dependency order
    op.drop_table("automation_target_state")
    op.drop_index("ix_automation_event_ts", table_name="automation_event")
    op.drop_index("ix_automation_event_automation_id", table_name="automation_event")
    op.drop_table("automation_event")
    op.drop_table("automation")
