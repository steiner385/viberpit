"""Comment-borne ownership markers (#3152): work_items.comment_owner_ids.

Revision ID: 0090_work_item_comment_owner_ids
Revises: 0089_work_item_author_id

Session ids named by ``owner-session`` markers in an issue's recent comments, so the
backlog runner honours a claim posted as a comment as well as one in the body.
Nullable, so ``init_db``'s reconcile ALTER-ADDs it on both dialects; this revision is
the reviewable record. Rows fill on their next mirror sync.
"""
import sqlalchemy as sa
from alembic import op

revision = "0090_work_item_comment_owner_ids"
down_revision = "0089_work_item_author_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("work_items", sa.Column("comment_owner_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("work_items", "comment_owner_ids")
