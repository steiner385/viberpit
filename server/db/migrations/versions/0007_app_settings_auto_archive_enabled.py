"""add app_settings.auto_archive_enabled — global kill switch for completion auto-archive

Revision ID: 0007_app_settings_auto_archive_enabled
Revises: 0006_fleet_hosts
Create Date: 2026-08-28

Adds one nullable boolean column (``auto_archive_enabled``) to ``app_settings``. It is
the app-wide toggle behind the "Auto-archive completed sessions" setting: when False,
a session that self-declares ``complete`` is left untouched (no archive, no nudge),
exactly like a per-session manual opt-out. NULL/True keep today's behavior, so the
column is nullable and every existing row and fresh install defaults to enabled.

Nothing runs ``alembic upgrade`` at deploy time — init_db()'s reconcile ALTER-ADDs this
nullable column on both SQLite and Postgres. This revision is the reviewable record.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_app_settings_auto_archive_enabled"
down_revision: Union[str, None] = "0006_fleet_hosts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("app_settings", sa.Column("auto_archive_enabled", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("app_settings", "auto_archive_enabled")
