"""Owner-evidence holds (#3113): work_items.hold_detail.

Revision ID: 0088_work_item_hold_detail
Revises: 0087_needs_you_hygiene

The structured detail of a hold, by kind — today the ``owner_evidence`` request
(``server/pm/holds.py:EvidenceRequest``). Nullable, so ``init_db``'s reconcile
ALTER-ADDs it on both dialects; this revision is the reviewable record.
"""
import sqlalchemy as sa
from alembic import op

revision = "0088_work_item_hold_detail"
down_revision = "0087_needs_you_hygiene"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("work_items", sa.Column("hold_detail", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("work_items", "hold_detail")
