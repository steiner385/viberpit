"""Add engine-neutral session identity and reasoning effort.

Existing rows are Claude Code sessions. The native conversation id is copied
verbatim before the legacy column is removed on managed migration paths. Runtime
startup performs the equivalent additive/backfill operation for deployments that
do not apply Alembic revisions.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

revision = "0024_session_engine"
down_revision = "0023_session_pause_reason"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    return {column["name"] for column in sa_inspect(op.get_bind()).get_columns("sessions")}


def upgrade() -> None:
    columns = _columns()
    if "engine" not in columns:
        op.add_column("sessions", sa.Column("engine", sa.String(), nullable=True))
    if "engine_session_id" not in columns:
        op.add_column("sessions", sa.Column("engine_session_id", sa.String(), nullable=True))
    if "reasoning_effort" not in columns:
        op.add_column("sessions", sa.Column("reasoning_effort", sa.String(), nullable=True))

    op.execute(sa.text(
        "UPDATE sessions SET engine = 'claude_code' WHERE engine IS NULL"
    ))
    columns = _columns()
    if "claude_session_id" in columns:
        op.execute(sa.text(
            "UPDATE sessions SET engine_session_id = claude_session_id "
            "WHERE engine_session_id IS NULL"
        ))
        op.drop_column("sessions", "claude_session_id")


def downgrade() -> None:
    columns = _columns()
    if "claude_session_id" not in columns:
        op.add_column("sessions", sa.Column("claude_session_id", sa.String(), nullable=True))
    if "engine_session_id" in columns:
        op.execute(sa.text(
            "UPDATE sessions SET claude_session_id = engine_session_id "
            "WHERE claude_session_id IS NULL"
        ))
        op.drop_column("sessions", "engine_session_id")
    columns = _columns()
    if "reasoning_effort" in columns:
        op.drop_column("sessions", "reasoning_effort")
    if "engine" in columns:
        op.drop_column("sessions", "engine")
