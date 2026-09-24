"""Field Core Web Vitals samples (#1411).

Revision ID: 0064_web_vital_samples
Revises: 0063_artifact_upstream_ref

``web_vital_samples`` stores one field measurement per (page load, metric):
LCP/INP/CLS/TTFB/FCP with the route SHAPE, device tier, browser family and
navigation type they were observed under, plus the count/bytes of JavaScript
fetched after load. No URL, identity, session id or User-Agent is stored — see
the model docstring; the absent columns are how that promise is kept.

Nothing runs ``alembic upgrade`` at deploy time: every column is nullable, so
``init_db``'s ``_reconcile_columns`` creates the table on both dialects. This
revision is the reviewable record of that change.
"""
from alembic import op
import sqlalchemy as sa

revision = "0064_web_vital_samples"
down_revision = "0063_artifact_upstream_ref"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_vital_samples",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("metric", sa.String(), nullable=True),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("route", sa.String(), nullable=True),
        sa.Column("device", sa.String(), nullable=True),
        sa.Column("browser", sa.String(), nullable=True),
        sa.Column("nav_type", sa.String(), nullable=True),
        sa.Column("app_version", sa.String(), nullable=True),
        sa.Column("client_id", sa.String(), nullable=True),
        sa.Column("lazy_chunks", sa.Integer(), nullable=True),
        sa.Column("lazy_chunk_bytes", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_web_vital_samples_metric", "web_vital_samples", ["metric"])
    op.create_index("ix_web_vital_samples_route", "web_vital_samples", ["route"])
    op.create_index("ix_web_vital_samples_created_at", "web_vital_samples", ["created_at"])
    op.create_index("ix_web_vital_samples_created_metric", "web_vital_samples",
                    ["created_at", "metric"])


def downgrade() -> None:
    op.drop_index("ix_web_vital_samples_created_metric", table_name="web_vital_samples")
    op.drop_index("ix_web_vital_samples_created_at", table_name="web_vital_samples")
    op.drop_index("ix_web_vital_samples_route", table_name="web_vital_samples")
    op.drop_index("ix_web_vital_samples_metric", table_name="web_vital_samples")
    op.drop_table("web_vital_samples")
