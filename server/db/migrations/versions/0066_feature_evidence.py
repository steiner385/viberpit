"""Capability evidence links (#2162).

Revision ID: 0066_feature_evidence
Revises: 0065_session_hand_back_reason

``feature_evidence`` indexes the repo paths a feature doc cites in its
``code:``/``tests:``/``specs:``/``adrs:`` frontmatter, with pm-sync's
resolution of each against the default-branch tree (``missing``,
``last_commit_at``). ``features.doc_committed_at`` is when the doc itself last
changed there. Roadmap health reads both to flag dangling evidence, live
capabilities with no tests, code with no spec, and a state older than its
newest spec.

Nothing runs ``alembic upgrade`` at deploy time: ``create_all`` creates the new
table and ``_reconcile_columns`` ALTER-ADDs the nullable column. This revision
is the reviewable record of that change.
"""
from alembic import op
import sqlalchemy as sa

revision = "0066_feature_evidence"
down_revision = "0065_session_hand_back_reason"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("features",
                  sa.Column("doc_committed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "feature_evidence",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("feature_id", sa.String(), sa.ForeignKey("features.id"), nullable=True),
        sa.Column("kind", sa.String(), nullable=True),
        sa.Column("path", sa.String(), nullable=True),
        sa.Column("missing", sa.Boolean(), nullable=True),
        sa.Column("last_commit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("feature_id", "kind", "path", name="uq_feature_evidence"),
    )
    op.create_index("ix_feature_evidence_feature_id", "feature_evidence", ["feature_id"])


def downgrade() -> None:
    op.drop_index("ix_feature_evidence_feature_id", table_name="feature_evidence")
    op.drop_table("feature_evidence")
    op.drop_column("features", "doc_committed_at")
