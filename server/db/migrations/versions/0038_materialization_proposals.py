"""materialization_proposals — reviewable plan decompositions (spec 003 §7b, #992).

Revision ID: 0038_materialization_proposals
Revises: 0037_triage_undo_snapshot
"""
from alembic import op
import sqlalchemy as sa

revision = "0038_materialization_proposals"
down_revision = "0037_triage_undo_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "materialization_proposals",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("initiative_id", sa.String(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        # Nullable with a Python-side default, exactly as the model declares it
        # (mirroring TriageProposal.status) — reconcile is what actually builds
        # this table, so the revision must describe what it builds.
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("rationale", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["initiative_id"], ["initiatives.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_materialization_proposals_initiative_id",
                    "materialization_proposals", ["initiative_id"])
    op.create_index("ix_materialization_proposals_status",
                    "materialization_proposals", ["status"])


def downgrade() -> None:
    op.drop_index("ix_materialization_proposals_status",
                  table_name="materialization_proposals")
    op.drop_index("ix_materialization_proposals_initiative_id",
                  table_name="materialization_proposals")
    op.drop_table("materialization_proposals")
