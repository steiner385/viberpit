"""Record each runner's advertised engine availability on its registry row.

Nullable and unbackfilled on purpose: NULL means "this host has not been probed
since the advertisement existed", which the edge reads as the legacy Claude-only
default for one fleet-upgrade window. Writing a synthetic value here would claim
knowledge the probe has not yet gathered.

Production applies the equivalent additive change through ``init_db``'s column
reconciliation; this revision is the reviewable record of it.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

revision = "0025_host_engines"
down_revision = "0024_session_engine"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    return {column["name"] for column in sa_inspect(op.get_bind()).get_columns("hosts")}


def upgrade() -> None:
    if "engines" not in _columns():
        op.add_column("hosts", sa.Column("engines", sa.JSON(), nullable=True))


def downgrade() -> None:
    if "engines" in _columns():
        op.drop_column("hosts", "engines")
