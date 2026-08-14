"""add automation.last_fired_at watermark for recurring-session dispatch

Revision ID: 0004_automation_last_fired_at
Revises: 0003_power_user_config_suite
Create Date: 2026-08-14

Adds one nullable column (``last_fired_at``) to the ``automation`` table. It is
the cron-dispatch watermark (#603): the most recent cron minute the recurring-
session scheduler already fired, persisted so a run is never double-fired across
a daemon restart. SQLite dev relies on init_db()/create_all + the ALTER-TABLE
backfill in server/db/__init__.py; this migration is the Postgres/prod path.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_automation_last_fired_at"
down_revision: Union[str, None] = "0003_power_user_config_suite"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("automation", sa.Column("last_fired_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("automation", "last_fired_at")
