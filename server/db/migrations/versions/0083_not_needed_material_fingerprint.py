"""Not-needed suppression (#2683): work_items.last_human_comment_at and
dispatch_outcomes.material_fingerprint.

Revision ID: 0083_not_needed_material_fingerprint
Revises: 0082_work_item_app_authors
"""
import sqlalchemy as sa
from alembic import op

revision = "0083_not_needed_material_fingerprint"
down_revision = "0082_work_item_app_authors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("work_items",
                  sa.Column("last_human_comment_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("dispatch_outcomes",
                  sa.Column("material_fingerprint", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("dispatch_outcomes", "material_fingerprint")
    op.drop_column("work_items", "last_human_comment_at")
