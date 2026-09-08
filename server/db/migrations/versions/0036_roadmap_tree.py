"""Roadmap drill-down Phase 2 (#993): capability tree and initiative↔capability links.

Revision ID: 0036_roadmap_tree
Revises: 0035_error_signatures
Create Date: 2026-09-07

Reviewable record only: nothing runs `alembic upgrade` at deploy time. The two
nullable ADDs are applied by init_db's _reconcile_columns on both dialects at
next boot. The DROPs are recorded here for the day a migration is applied by
hand; reconcile never drops, and the drift assert only checks for MISSING
columns, so the leftover DB columns/table are harmless until then.
"""
from alembic import op
import sqlalchemy as sa

revision = "0036_roadmap_tree"
down_revision = "0035_error_signatures"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("features", sa.Column(
        "parent_feature_id", sa.String(), sa.ForeignKey("features.id"), nullable=True))
    op.create_index("ix_features_parent_feature_id", "features", ["parent_feature_id"])
    op.add_column("initiative_features", sa.Column("is_primary", sa.Boolean(), nullable=True))
    with op.batch_alter_table("initiatives") as batch:
        for col in ("reach", "impact", "effort", "confidence"):
            batch.drop_column(col)
    op.drop_table("initiative_repos")


def downgrade() -> None:
    op.create_table(
        "initiative_repos",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("initiative_id", sa.String(), sa.ForeignKey("initiatives.id"),
                  nullable=True, index=True),
        sa.Column("repo", sa.String(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("initiative_id", "repo", name="uq_initiative_repo"),
    )
    with op.batch_alter_table("initiatives") as batch:
        for col in ("reach", "impact", "effort", "confidence"):
            batch.add_column(sa.Column(col, sa.Integer(), nullable=True))
    op.drop_column("initiative_features", "is_primary")
    op.drop_index("ix_features_parent_feature_id", table_name="features")
    op.drop_column("features", "parent_feature_id")
