"""003 Phase C: mirror GitHub assignee logins onto work_items.

Record-only: nothing runs ``alembic upgrade`` at deploy time (see AGENTS/CLAUDE.md
"Dev / testing"). The schema reconcile ALTER-ADDs this nullable column automatically
on both SQLite and Postgres; this revision is the reviewable record of the change.
"""
import sqlalchemy as sa
from alembic import op

revision = "0021_work_item_assignees"
down_revision = "0020_journal_mutation_id_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("work_items", sa.Column("assignees", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("work_items", "assignees")
