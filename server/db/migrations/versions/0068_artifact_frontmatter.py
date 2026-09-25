"""Artifact frontmatter contract (#2161).

Revision ID: 0068_artifact_frontmatter
Revises: 0067_archive_review_finding

Three nullable columns on ``initiative_artifacts`` back-fill from the
``docs/sdlc/<slug>/<kind>.md`` frontmatter fence pm-sync now reads:
``frontmatter_present`` (was a fence found at all), ``frontmatter_epic`` (the
``epic:`` value verbatim), ``frontmatter_capabilities`` (the ``capabilities:``
list verbatim). See ``server/pm/artifactdoc.py``.

Nothing runs ``alembic upgrade`` at deploy time: ``init_db``'s
``_reconcile_columns`` ALTER-ADDs nullable columns on both dialects. This
revision is the reviewable record of that change.
"""
from alembic import op
import sqlalchemy as sa

revision = "0068_artifact_frontmatter"
down_revision = "0067_archive_review_finding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("initiative_artifacts",
                  sa.Column("frontmatter_present", sa.Boolean(), nullable=True))
    op.add_column("initiative_artifacts",
                  sa.Column("frontmatter_epic", sa.String(), nullable=True))
    op.add_column("initiative_artifacts",
                  sa.Column("frontmatter_capabilities", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("initiative_artifacts", "frontmatter_capabilities")
    op.drop_column("initiative_artifacts", "frontmatter_epic")
    op.drop_column("initiative_artifacts", "frontmatter_present")
