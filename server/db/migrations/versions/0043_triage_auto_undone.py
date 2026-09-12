"""triage_auto_undone: auto_undone on triage_proposals

Revision ID: 0043_triage_auto_undone
Revises: 0042_product_automation_policy

Reviewable record (spec 2026-09-12-product-automation-policy-design §8/§10). Nothing
runs `alembic upgrade` at deploy time: init_db's reconcile adds the nullable column.
"""
from alembic import op
import sqlalchemy as sa

revision = "0043_triage_auto_undone"
down_revision = "0042_product_automation_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("triage_proposals", sa.Column("auto_undone", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("triage_proposals", "auto_undone")
