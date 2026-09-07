"""SDLC Phase 2 (#804): triage_items — the control-band incident queue.

Revision ID: 0033_triage_items
Revises: 0032_session_feedback
Create Date: 2026-09-06

Reviewable record only: nothing runs `alembic upgrade` at deploy time — init_db's
create_all builds the new table on both SQLite and Postgres at next boot, and
_reconcile_columns ALTER-ADDs any later nullable column.
"""
from alembic import op
import sqlalchemy as sa

revision = "0033_triage_items"
down_revision = "0032_session_feedback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "triage_items",
        sa.Column("id", sa.String(), primary_key=True),
        # Breach identity (metric:rule:side) — unique so a persisting breach
        # updates its row instead of filing a duplicate on every tick.
        sa.Column("key", sa.String(), nullable=True, unique=True),
        sa.Column("metric", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("repo", sa.String(), nullable=True),
        sa.Column("rule", sa.String(), nullable=True),
        sa.Column("sigma", sa.Integer(), nullable=True),
        sa.Column("side", sa.String(), nullable=True),
        sa.Column("tier", sa.String(), nullable=True),
        sa.Column("response", sa.String(), nullable=True),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("baseline_mean", sa.Float(), nullable=True),
        sa.Column("baseline_stddev", sa.Float(), nullable=True),
        sa.Column("baseline_n", sa.Integer(), nullable=True),
        sa.Column("observed_at", sa.String(), nullable=True),
        sa.Column("summary", sa.String(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("dismissed_reason", sa.String(), nullable=True),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("initiative_slug", sa.String(), nullable=True),
        sa.Column("occurrences", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_triage_items_key", "triage_items", ["key"])
    op.create_index("ix_triage_items_status", "triage_items", ["status"])


def downgrade() -> None:
    op.drop_index("ix_triage_items_status", table_name="triage_items")
    op.drop_index("ix_triage_items_key", table_name="triage_items")
    op.drop_table("triage_items")
