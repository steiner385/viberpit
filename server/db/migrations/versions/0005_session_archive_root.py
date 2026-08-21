"""add sessions.archive_root so archived sessions resolve to the root they were written to

Revision ID: 0005_session_archive_root
Revises: 0004_automation_last_fired_at
Create Date: 2026-08-21

Adds one nullable column (``archive_root``) to the ``sessions`` table. ``archive_home``
is a PLACEMENT setting (which disk archives live on) while being archived is a LIFECYCLE
fact about a session; fusing the two meant that repointing CCSWITCH_ARCHIVE_HOME at a
different disk retroactively changed where every past archive was believed to live, and
unarchive silently recreated an empty cwd instead of restoring the work.

archive_session now records the root it used and unarchive resolves through it, falling
back to the current archive home only for legacy rows where this column is NULL. Backfill
those rows with scripts/ccswitch-backfill-archive-root.py.

SQLite dev relies on init_db()/create_all + the ALTER-TABLE backfill in
server/db/__init__.py; this migration is the Postgres/prod path.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_session_archive_root"
down_revision: Union[str, None] = "0004_automation_last_fired_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("archive_root", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "archive_root")
