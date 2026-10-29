"""Edge hand-off flag for Archive Audit reopens (#3142):
archive_review_finding.awaiting_edge_unarchive.

Revision ID: 0092_archive_finding_awaiting_edge
Revises: 0091_assistant_notices

Marks an auto-reopened finding whose session lives on a worker the primary daemon
cannot dial; the edge's pass restores it and clears the flag. Nullable, so
``init_db``'s reconcile ALTER-ADDs it on both dialects; this revision is the
reviewable record.
"""
import sqlalchemy as sa
from alembic import op

revision = "0092_archive_finding_awaiting_edge"
down_revision = "0091_assistant_notices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("archive_review_finding",
                  sa.Column("awaiting_edge_unarchive", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("archive_review_finding", "awaiting_edge_unarchive")
