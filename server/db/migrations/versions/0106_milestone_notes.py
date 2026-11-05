"""Release milestones Phase 2: milestone_notes (owner decisions and owner asks).

Revision ID: 0106_milestone_notes
Revises: 0105_release_milestones

``milestone_notes`` holds a ladder's owner decisions (``locked | proposed | superseded``)
and owner asks (``open | done | dropped``); ``milestone_key`` NULL is ladder-wide.
``init_db``'s ``create_all`` creates it on both dialects; this revision is the
reviewable record.

Chaining note: chained off 0105_release_milestones (master's newest revision when written).
If another revision lands first, re-chain ``down_revision`` to the newest one
(``test_alembic_single_head`` enforces one head).
"""
import sqlalchemy as sa
from alembic import op

revision = "0106_milestone_notes"
down_revision = "0105_release_milestones"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "milestone_notes",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("ladder_id", sa.String(), sa.ForeignKey("milestone_ladders.id"),
                  nullable=False),
        sa.Column("milestone_key", sa.String(), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("ref", sa.String(), nullable=True),
        sa.Column("text", sa.String(), nullable=True),
        sa.Column("url", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_by", sa.String(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_milestone_notes_ladder_id", "milestone_notes", ["ladder_id"])
    op.create_index("ix_milestone_notes_kind_status", "milestone_notes",
                    ["ladder_id", "kind", "status"])


def downgrade() -> None:
    op.drop_index("ix_milestone_notes_kind_status", table_name="milestone_notes")
    op.drop_index("ix_milestone_notes_ladder_id", table_name="milestone_notes")
    op.drop_table("milestone_notes")
