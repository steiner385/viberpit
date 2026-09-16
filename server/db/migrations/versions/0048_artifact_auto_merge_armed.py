"""Artifact auto-merge arming stamp (roadmap auto-execution phase 2b).

Revision ID: 0048_artifact_auto_merge_armed
Revises: 0047_roadmap_auto_execution

``initiative_artifacts.auto_merge_armed_at`` (nullable datetime) records that the
SDLC synthesizer armed GitHub auto-merge on this artifact's PR, so a later tick
never spends a second ``gh`` call on it.

Nothing runs ``alembic upgrade`` at deploy time: ``init_db``'s
``_reconcile_columns`` ALTER-ADDs the nullable column on both dialects. This
revision is the reviewable record of that change.
"""
from alembic import op
import sqlalchemy as sa

revision = "0048_artifact_auto_merge_armed"
down_revision = "0047_roadmap_auto_execution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("initiative_artifacts",
                  sa.Column("auto_merge_armed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("initiative_artifacts", "auto_merge_armed_at")
