"""003 Phase B: reconciler schema — WorkItem.gh_node_id, GhWriteJournal.batch_id."""
import sqlalchemy as sa
from alembic import op

revision = "0019_reconciler"
down_revision = "0018_gh_projects_mirror"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("work_items", sa.Column("gh_node_id", sa.String(), nullable=True))
    op.create_index("ix_work_items_gh_node_id", "work_items", ["gh_node_id"])
    op.add_column("gh_write_journal", sa.Column("batch_id", sa.String(), nullable=True))
    op.create_index("ix_gh_write_journal_batch_id", "gh_write_journal", ["batch_id"])


def downgrade() -> None:
    op.drop_index("ix_gh_write_journal_batch_id", table_name="gh_write_journal")
    op.drop_column("gh_write_journal", "batch_id")
    op.drop_index("ix_work_items_gh_node_id", table_name="work_items")
    op.drop_column("work_items", "gh_node_id")
