"""product_automation_policy: automation_policy, automation_policy_updated_at on projects

Revision ID: 0041_product_automation_policy
Revises: 0040_feature_map_changes

Reviewable record of the schema change (spec 2026-09-12-product-automation-policy-design §4).
Nothing runs `alembic upgrade` at deploy time: init_db's reconcile adds both nullable
columns on SQLite and Postgres.
"""
from alembic import op
import sqlalchemy as sa

revision = "0041_product_automation_policy"
down_revision = "0040_feature_map_changes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("automation_policy", sa.JSON(), nullable=True))
    op.add_column(
        "projects",
        sa.Column("automation_policy_updated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "automation_policy_updated_at")
    op.drop_column("projects", "automation_policy")
