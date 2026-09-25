"""Persona layer in the planning spine (issue #2164): personas + persona_features
tables, and features.persona_slugs.

Revision ID: 0070_persona
Revises: 0069_archive_review_finding_auto_reopen_failed

Nothing runs `alembic upgrade` at deploy time: `create_all` creates the two new
tables and `_reconcile_columns` ALTER-ADDs the nullable `persona_slugs` column
on both dialects. This revision is the reviewable record of that change.
"""
from alembic import op
import sqlalchemy as sa

revision = "0070_persona"
down_revision = "0069_archive_review_finding_auto_reopen_failed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "personas" not in existing:
        op.create_table(
            "personas",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=True),
            sa.Column("slug", sa.String(), nullable=True),
            sa.Column("title", sa.String(), nullable=True),
            sa.Column("role", sa.String(), nullable=True),
            sa.Column("age_band", sa.String(), nullable=True),
            sa.Column("tier", sa.String(), nullable=True),
            sa.Column("summary", sa.String(), nullable=True),
            sa.Column("repo_path", sa.String(), nullable=True),
            sa.Column("orphaned", sa.Boolean(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("project_id", "slug", name="uq_persona_slug"),
        )
    if "persona_features" not in existing:
        op.create_table(
            "persona_features",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("persona_id", sa.String(), sa.ForeignKey("personas.id"), nullable=True),
            sa.Column("feature_id", sa.String(), sa.ForeignKey("features.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("persona_id", "feature_id", name="uq_persona_feature"),
        )
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("features")}
    if "persona_slugs" not in columns:
        op.add_column("features", sa.Column("persona_slugs", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("features", "persona_slugs")
    op.drop_table("persona_features")
    op.drop_table("personas")
