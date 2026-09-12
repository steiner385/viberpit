"""Initiative nesting + feature route labels (roadmap 5S plan, 2026-09-12).

Nothing runs `alembic upgrade` at deploy time; both columns are nullable, so
init_db's `_reconcile_columns` ALTER-ADDs them on SQLite and Postgres at next
boot. This revision is the reviewable record of the change.

Revision ID: 0041_initiative_nesting_route_labels
Revises: 0040_feature_map_changes
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0041_initiative_nesting_route_labels"
down_revision = "0040_feature_map_changes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("initiatives", sa.Column(
        "parent_initiative_id", sa.String(), sa.ForeignKey("initiatives.id"), nullable=True))
    op.create_index("ix_initiatives_parent_initiative_id", "initiatives", ["parent_initiative_id"])
    op.add_column("features", sa.Column("route_labels", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("features", "route_labels")
    op.drop_index("ix_initiatives_parent_initiative_id", table_name="initiatives")
    op.drop_column("initiatives", "parent_initiative_id")
