"""add composite partial index on app_sessions(identity, created_at DESC)

Revision ID: 0010_app_sessions_composite_index
Revises: 0009_sdlc_initiatives
Create Date: 2026-08-30

``app_sessions`` seq-scanned ~42,518 rows per authenticated request (253,446
total seq-scans observed in pg_stat_user_tables) because the reuse query

    SELECT … WHERE identity=? AND revoked_at IS NULL AND expires_at>?
    ORDER BY created_at DESC LIMIT 1

hit the old single-column ``ix_app_sessions_identity`` index, which provided
near-zero selectivity (~4 distinct values across 42k rows) and could not serve
the ORDER BY — so Postgres fell back to a seq-scan + sort on every call at
~35 req/min.  (#830, sibling of #827/#791)

The replacement is a composite partial index
    (identity, created_at DESC) WHERE revoked_at IS NULL
which lets Postgres descend straight to the newest live session row for an
identity without touching expired or revoked history at all.

IMPORTANT — this does NOT self-apply.  ``init_db``'s reconcile ALTER-ADDs
missing *columns*; it does not create indexes, and ``create_all`` never touches
an existing table.  Nothing runs ``alembic upgrade`` at deploy time either.
On a live database this MUST be created by hand, CONCURRENTLY so it does not
lock the table the daemon is actively writing:

    -- Drop the now-redundant single-column index first (optional but avoids
    -- redundant scan plans):
    DROP INDEX CONCURRENTLY IF EXISTS ix_app_sessions_identity;

    -- Create the replacement composite partial index:
    CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_app_sessions_identity_created_at
        ON app_sessions (identity, created_at DESC)
        WHERE revoked_at IS NULL;

(CONCURRENTLY cannot run inside a transaction, so it cannot be expressed in
op.create_index below — that form is for fresh databases built by create_all.)
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010_app_sessions_composite_index"
down_revision: Union[str, None] = "0009_sdlc_initiatives"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop the superseded single-column index (may not exist on fresh DBs built
    # after the model change, so use IF EXISTS).
    op.execute("DROP INDEX IF EXISTS ix_app_sessions_identity")
    # Partial index: only live (non-revoked) sessions need fast identity lookup.
    # The postgresql_where clause is Postgres-only; SQLite ignores it and creates
    # a regular composite index (which is still far better than the old single-col).
    op.create_index(
        "ix_app_sessions_identity_created_at",
        "app_sessions",
        ["identity", sa.text("created_at DESC")],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_app_sessions_identity_created_at", table_name="app_sessions")
    op.create_index("ix_app_sessions_identity", "app_sessions", ["identity"])
