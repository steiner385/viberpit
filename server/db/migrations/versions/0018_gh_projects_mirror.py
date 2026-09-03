"""003 Phase A: Projects v2 mirror tables + epic overlay columns."""
import sqlalchemy as sa
from alembic import op

revision = "0018_gh_projects_mirror"
down_revision = "0017_deploy_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gh_projects",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("gh_project_number", sa.Integer(), nullable=True),
        sa.Column("gh_node_id", sa.String(), nullable=True, unique=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("field_cache", sa.JSON(), nullable=True),
        sa.Column("watermark", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_gh_projects_project_id", "gh_projects", ["project_id"])
    op.create_table(
        "gh_project_items",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("gh_project_id", sa.String(), sa.ForeignKey("gh_projects.id"), nullable=True),
        sa.Column("work_item_id", sa.String(), sa.ForeignKey("work_items.id"), nullable=True),
        sa.Column("gh_item_node_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("stage", sa.String(), nullable=True),
        sa.Column("gh_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("gh_project_id", "gh_item_node_id", name="uq_gh_project_item"),
    )
    op.create_index("ix_gh_project_items_status", "gh_project_items", ["status"])
    op.create_index("ix_gh_project_items_gh_project_id", "gh_project_items", ["gh_project_id"])
    op.create_index("ix_gh_project_items_work_item_id", "gh_project_items", ["work_item_id"])
    op.create_table(
        "work_item_deps",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("src_work_item_id", sa.String(), sa.ForeignKey("work_items.id"), nullable=True),
        sa.Column("dst_work_item_id", sa.String(), sa.ForeignKey("work_items.id"), nullable=True),
        sa.Column("src_ref", sa.String(), nullable=True),
        sa.Column("dst_ref", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("src_ref", "dst_ref", "source", name="uq_work_item_dep"),
    )
    op.create_index("ix_work_item_deps_src_ref", "work_item_deps", ["src_ref"])
    op.create_index("ix_work_item_deps_src_work_item_id", "work_item_deps", ["src_work_item_id"])
    op.create_index("ix_work_item_deps_dst_work_item_id", "work_item_deps", ["dst_work_item_id"])
    op.create_table(
        "gh_write_journal",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("target_node_id", sa.String(), nullable=True),
        sa.Column("field", sa.String(), nullable=True),
        sa.Column("old_value", sa.JSON(), nullable=True),
        sa.Column("new_value", sa.JSON(), nullable=True),
        sa.Column("mutation_id", sa.String(), nullable=True),
        sa.Column("initiated_by", sa.String(), nullable=True),
        sa.Column("applied", sa.Boolean(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_gh_write_journal_target", "gh_write_journal", ["target_node_id"])
    op.add_column("work_items", sa.Column("parent_work_item_id", sa.String(), nullable=True))
    op.add_column("work_items", sa.Column("is_epic", sa.Boolean(), nullable=True))
    op.add_column("initiatives", sa.Column("epic_repo", sa.String(), nullable=True))
    op.add_column("initiatives", sa.Column("epic_number", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("initiatives", "epic_number")
    op.drop_column("initiatives", "epic_repo")
    op.drop_column("work_items", "is_epic")
    op.drop_column("work_items", "parent_work_item_id")
    op.drop_table("gh_write_journal")
    op.drop_index("ix_work_item_deps_dst_work_item_id", "work_item_deps")
    op.drop_index("ix_work_item_deps_src_work_item_id", "work_item_deps")
    op.drop_table("work_item_deps")
    op.drop_index("ix_gh_project_items_work_item_id", "gh_project_items")
    op.drop_index("ix_gh_project_items_gh_project_id", "gh_project_items")
    op.drop_table("gh_project_items")
    op.drop_table("gh_projects")
