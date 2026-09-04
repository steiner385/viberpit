"""PM product layer — projects/features/work-item mirror + initiative links.

Revision ID: 0014_pm_product_layer
Revises: 0013_host_upgrade_ssh_config
Create Date: 2026-09-01

Design: docs/superpowers/specs/2026-08-31-pm-product-layer-design.md (§3).
Deploy note: nothing runs ``alembic upgrade`` at deploy time. The NEW tables
are built by ``init_db``'s create_all; the columns added to ``initiatives``
are ALTER-ADDed by _reconcile_columns (all nullable). This revision is the
reviewable record of the change.
"""
from alembic import op
import sqlalchemy as sa

revision = "0014_pm_product_layer"
down_revision = "0013_host_upgrade_ssh_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("slug", sa.String(), nullable=True, unique=True, index=True),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("portfolio_tags", sa.JSON(), nullable=True),
        sa.Column("onboarding_state", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "project_repos",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"),
                  nullable=True, index=True),
        sa.Column("repo", sa.String(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=True),
        sa.Column("backfill_cursor", sa.String(), nullable=True),
        sa.Column("backfill_phase", sa.String(), nullable=True),
        sa.Column("backfill_done", sa.Boolean(), nullable=True),
        sa.Column("watermark", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sync_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "repo", name="uq_project_repo"),
    )
    op.create_table(
        "project_edges",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("src_project_id", sa.String(), sa.ForeignKey("projects.id"),
                  nullable=True, index=True),
        sa.Column("dst_project_id", sa.String(), sa.ForeignKey("projects.id"),
                  nullable=True, index=True),
        sa.Column("kind", sa.String(), nullable=True),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("src_project_id", "dst_project_id", "kind",
                            name="uq_project_edge"),
    )
    op.create_table(
        "features",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"),
                  nullable=True, index=True),
        sa.Column("slug", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("state", sa.String(), nullable=True),
        sa.Column("summary", sa.String(), nullable=True),
        sa.Column("repo_path", sa.String(), nullable=True),
        sa.Column("commit_sha", sa.String(), nullable=True),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("pr_url", sa.String(), nullable=True),
        sa.Column("orphaned", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "slug", name="uq_feature_slug"),
    )
    op.create_table(
        "feature_links",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("src_feature_id", sa.String(), sa.ForeignKey("features.id"),
                  nullable=True, index=True),
        sa.Column("dst_feature_id", sa.String(), sa.ForeignKey("features.id"),
                  nullable=True, index=True),
        sa.Column("kind", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("src_feature_id", "dst_feature_id", "kind",
                            name="uq_feature_link"),
    )
    op.create_table(
        "work_items",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("repo", sa.String(), nullable=True, index=True),
        sa.Column("number", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("body", sa.String(), nullable=True),
        sa.Column("state", sa.String(), nullable=True),
        sa.Column("labels", sa.JSON(), nullable=True),
        sa.Column("author", sa.String(), nullable=True),
        sa.Column("url", sa.String(), nullable=True),
        sa.Column("linked_issue_numbers", sa.JSON(), nullable=True),
        sa.Column("gh_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("gh_updated_at", sa.DateTime(timezone=True), nullable=True, index=True),
        sa.Column("gh_closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("feature_id", sa.String(), sa.ForeignKey("features.id"),
                  nullable=True, index=True),
        sa.Column("reach", sa.Integer(), nullable=True),
        sa.Column("impact", sa.Integer(), nullable=True),
        sa.Column("effort", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.Integer(), nullable=True),
        sa.Column("triage_state", sa.String(), nullable=True, index=True),
        sa.Column("classified_by", sa.String(), nullable=True),
        sa.Column("classified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("repo", "number", "kind", name="uq_work_item_identity"),
    )
    op.create_table(
        "triage_proposals",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("work_item_id", sa.String(), sa.ForeignKey("work_items.id"),
                  nullable=True, index=True),
        sa.Column("proposed_feature_id", sa.String(), sa.ForeignKey("features.id"),
                  nullable=True),
        sa.Column("no_fit_suggestion", sa.String(), nullable=True),
        sa.Column("reach", sa.Integer(), nullable=True),
        sa.Column("impact", sa.Integer(), nullable=True),
        sa.Column("effort", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.Integer(), nullable=True),
        sa.Column("rationale", sa.String(), nullable=True),
        sa.Column("confidence_level", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=True, index=True),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
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
    op.create_table(
        "initiative_features",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("initiative_id", sa.String(), sa.ForeignKey("initiatives.id"),
                  nullable=True, index=True),
        sa.Column("feature_id", sa.String(), sa.ForeignKey("features.id"),
                  nullable=True, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("initiative_id", "feature_id", name="uq_initiative_feature"),
    )
    op.add_column("initiatives", sa.Column("project_id", sa.String(),
                  sa.ForeignKey("projects.id"), nullable=True))
    op.add_column("initiatives", sa.Column("reach", sa.Integer(), nullable=True))
    op.add_column("initiatives", sa.Column("impact", sa.Integer(), nullable=True))
    op.add_column("initiatives", sa.Column("effort", sa.Integer(), nullable=True))
    op.add_column("initiatives", sa.Column("confidence", sa.Integer(), nullable=True))


def downgrade() -> None:
    for col in ("confidence", "effort", "impact", "reach", "project_id"):
        op.drop_column("initiatives", col)
    for table in ("initiative_features", "initiative_repos", "triage_proposals",
                  "work_items", "feature_links", "features", "project_edges",
                  "project_repos", "projects"):
        op.drop_table(table)
