"""Trust by immutable user id (#3147): work_items.author_id.

Revision ID: 0089_work_item_author_id
Revises: 0088_work_item_hold_detail

The GitHub databaseId of a User author (NULL for Bots, ghost accounts and rows not yet
re-synced). Nullable, so ``init_db``'s reconcile ALTER-ADDs it on both dialects; this
revision is the reviewable record. The data refill (re-mirror every repo once) is
``server.db._backfill_work_item_author_ids``, the path production actually runs.
"""
import sqlalchemy as sa
from alembic import op

revision = "0089_work_item_author_id"
down_revision = "0088_work_item_hold_detail"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("work_items", sa.Column("author_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("work_items", "author_id")
