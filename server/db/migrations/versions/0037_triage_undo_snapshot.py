"""triage_proposals.undo_snapshot — reversible accept/reject (finish-line row 3).

Revision ID: 0037_triage_undo_snapshot
Revises: 0036_roadmap_tree
"""
from alembic import op
import sqlalchemy as sa

revision = "0037_triage_undo_snapshot"
down_revision = "0036_roadmap_tree"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("triage_proposals", sa.Column("undo_snapshot", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("triage_proposals", "undo_snapshot")
