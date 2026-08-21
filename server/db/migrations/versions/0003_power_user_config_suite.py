"""power-user config suite: automation schedule, notification rules/channels, quiet hours

Revision ID: 0003_power_user_config_suite
Revises: 0002_automation_registry
Create Date: 2026-07-16

Adds, for issues #601-#604:
* ``automation.schedule`` — optional per-automation cadence (JSON, nullable).
* ``notification_channel`` + ``notification_rule`` tables — server-side
  notification routing (webhook channels + per-event rules).
* ``app_settings.quiet_hours_start`` / ``quiet_hours_end`` — quiet-hours window.

SQLite dev relies on init_db()/create_all + _reconcile_columns; this
migration is the Postgres/prod path for the same schema changes.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_power_user_config_suite"
down_revision: Union[str, None] = "0002_automation_registry"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- automation schedule column (#603) ---
    op.add_column("automation", sa.Column("schedule", sa.JSON(), nullable=True))

    # --- quiet-hours window on the singleton settings row (#602) ---
    op.add_column("app_settings", sa.Column("quiet_hours_start", sa.String(), nullable=True))
    op.add_column("app_settings", sa.Column("quiet_hours_end", sa.String(), nullable=True))

    # --- notification_channel table (#602) ---
    op.create_table(
        "notification_channel",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False, server_default="webhook"),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("headers", sa.JSON(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    # --- notification_rule table (#602) ---
    op.create_table(
        "notification_rule",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("event_class", sa.String(), nullable=False, server_default="*"),
        sa.Column("scope_repo", sa.String(), nullable=True),
        sa.Column("scope_account", sa.String(), nullable=True),
        sa.Column("scope_automation", sa.String(), nullable=True),
        sa.Column("action", sa.String(), nullable=False, server_default="push"),
        sa.Column("channel_id", sa.String(), nullable=True),
        sa.Column("critical", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("notification_rule")
    op.drop_table("notification_channel")
    op.drop_column("app_settings", "quiet_hours_end")
    op.drop_column("app_settings", "quiet_hours_start")
    op.drop_column("automation", "schedule")
