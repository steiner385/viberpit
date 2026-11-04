"""Durable per-grant MCP refresh health (MCP auth overhaul phase 3): mcp_grant_health.

Revision ID: 0101_mcp_grant_health
Revises: 0098_session_memory_files

One row per shared-store grant key: the last refresh attempt/outcome/error, the last
success, the failure streak, and ``alerted_for`` (``f"{outcome}:{generation}"``) so a dead
or expired grant pushes once per generation. Every non-PK column is nullable; ``init_db``'s
create_all installs the table and reconcile covers later columns on both dialects — this
revision is the reviewable record.

Chaining note: open PR #3263 holds 0099/0100 off 0098. If it lands first, re-chain
``down_revision`` to its newest revision (``test_alembic_single_head`` enforces one head).
"""
import sqlalchemy as sa
from alembic import op

revision = "0101_mcp_grant_health"
down_revision = "0098_session_memory_files"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_grant_health",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("server", sa.String(), nullable=True),
        sa.Column("state", sa.String(), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_ok_at", sa.DateTime(), nullable=True),
        sa.Column("last_outcome", sa.String(), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=True),
        sa.Column("alerted_for", sa.String(), nullable=True),
        sa.Column("alerted_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("mcp_grant_health")
