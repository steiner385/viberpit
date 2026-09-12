"""feature_map_changes: pointer to a product's ccswitch-owned map-changes PR

Revision ID: 0040_feature_map_changes
Revises: 0039_child_sessions

Reviewable record of the schema change (roadmap drill-down spec §9/§10).
Nothing runs `alembic upgrade` at deploy time: init_db's create_all creates the
table on both dialects; every column is nullable or defaulted.
"""
from alembic import op
import sqlalchemy as sa

revision = "0040_feature_map_changes"
down_revision = "0039_child_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feature_map_changes",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=True, index=True),
        sa.Column("repo", sa.String(), nullable=True),
        sa.Column("branch", sa.String(), nullable=True),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("pr_url", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("head_sha", sa.String(), nullable=True),
        sa.Column("behind", sa.Boolean(), nullable=True),
        sa.Column("conflicting", sa.Boolean(), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("feature_map_changes")
