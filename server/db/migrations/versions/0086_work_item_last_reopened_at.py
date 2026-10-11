"""Not-needed suppression re-arm (#2683): work_items.last_reopened_at.

Revision ID: 0086_work_item_last_reopened_at
Revises: 0085_permission_rule_scopes
"""
import sqlalchemy as sa
from alembic import op

revision = "0086_work_item_last_reopened_at"
down_revision = "0085_permission_rule_scopes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("work_items",
                  sa.Column("last_reopened_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("work_items", "last_reopened_at")
