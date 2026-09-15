"""Roadmap auto-execution phase 2: record what the machine did with a gate.

Revision ID: 0046_roadmap_auto_execution
Revises: 0045_automation_ownership

``initiatives.last_auto_gate`` (JSON, nullable) carries the payload
``server/sdlc/gates.py:auto_gate_record`` builds — {gate, at, tolerance,
outcome, detail} — overwritten by each auto-resolution (spec 2026-09-14 §8).
Nullable, so ``init_db``'s ``_reconcile_columns`` also adds it on both dialects;
this revision is the reviewable record of the change.
"""
from alembic import op
import sqlalchemy as sa

revision = "0046_roadmap_auto_execution"
down_revision = "0045_automation_ownership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("initiatives", sa.Column("last_auto_gate", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("initiatives", "last_auto_gate")
