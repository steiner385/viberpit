"""Artifact upstream-ref provenance (#2159).

Revision ID: 0063_artifact_upstream_ref
Revises: 0062_project_constitution_status

``initiative_artifacts.upstream_ref`` (nullable string) records what actually
seeded the prompt when it was not the normal committed upstream file — e.g.
``"owner/repo#42"`` for an epic-adoption initiative's spec stage, which has no
committed ``intent.md`` to read.

Nothing runs ``alembic upgrade`` at deploy time: ``init_db``'s
``_reconcile_columns`` ALTER-ADDs the nullable column on both dialects. This
revision is the reviewable record of that change.
"""
from alembic import op
import sqlalchemy as sa

revision = "0063_artifact_upstream_ref"
down_revision = "0062_project_constitution_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("initiative_artifacts",
                  sa.Column("upstream_ref", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("initiative_artifacts", "upstream_ref")
