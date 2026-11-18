"""Product repo lifecycle: project_repos retire columns + moved_issue_refs.

Revision ID: 0112_product_repo_lifecycle
Revises: 0111_session_sandbox

``project_repos.retired_at`` / ``retired_reason`` / ``superseded_by`` (all nullable) and
the ``moved_issue_refs`` table (every non-key column nullable). ``init_db`` reconcile
applies them on both dialects; this revision is the reviewable record.
"""
import sqlalchemy as sa
from alembic import op

revision = "0112_product_repo_lifecycle"
down_revision = "0111_session_sandbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("project_repos", sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("project_repos", sa.Column("retired_reason", sa.String(), nullable=True))
    op.add_column("project_repos", sa.Column("superseded_by", sa.String(), nullable=True))
    op.create_table(
        "moved_issue_refs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("old_ref", sa.String(), nullable=True),
        sa.Column("new_ref", sa.String(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("migrated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("migration", sa.JSON(), nullable=True),
    )
    op.create_index("ix_moved_issue_refs_project_id", "moved_issue_refs", ["project_id"])
    op.create_index("ix_moved_issue_refs_old_ref", "moved_issue_refs", ["old_ref"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_moved_issue_refs_old_ref", table_name="moved_issue_refs")
    op.drop_index("ix_moved_issue_refs_project_id", table_name="moved_issue_refs")
    op.drop_table("moved_issue_refs")
    op.drop_column("project_repos", "superseded_by")
    op.drop_column("project_repos", "retired_reason")
    op.drop_column("project_repos", "retired_at")
