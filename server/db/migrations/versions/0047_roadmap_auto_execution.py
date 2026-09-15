"""Roadmap auto-execution phase 2: record what the machine did with a gate.

Revision ID: 0047_roadmap_auto_execution
Revises: 0046_pr_resolution_source_harvest

``initiatives.last_auto_gate`` (JSON, nullable) carries the payload
``server/sdlc/gates.py:auto_gate_record`` builds — {gate, at, tolerance,
outcome, detail} — overwritten by each auto-resolution (spec 2026-09-14 §8).
Nullable, so ``init_db``'s ``_reconcile_columns`` also adds it on both dialects;
this revision is the reviewable record of the change.
"""
from alembic import op
import sqlalchemy as sa

revision = "0047_roadmap_auto_execution"
# Renumbered from 0046: #1758's `0046_pr_resolution_source_harvest` and this one
# both chained off 0045 and merged without a git conflict, leaving alembic two
# heads and master red. Chain off master's newest revision, never off 0045 again.
down_revision = "0046_pr_resolution_source_harvest"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("initiatives", sa.Column("last_auto_gate", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("initiatives", "last_auto_gate")
