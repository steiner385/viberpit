"""Durable canonical automation ownership (no expiry or fail-open).

Revision ID: 0045_automation_ownership
Revises: 0044_dispatch_outcomes

New tables are also created by init_db/create_all. Retry/outcome history remains
in automation_target_state; callers migrate live exclusion to these tables.
"""
from alembic import op
import sqlalchemy as sa

revision = "0045_automation_ownership"
down_revision = "0044_dispatch_outcomes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "automation_operations",
        sa.Column("token", sa.String(), primary_key=True),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("targets", sa.JSON(), nullable=False),
        sa.Column("display_refs", sa.JSON(), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("phase", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("reserved_session_id", sa.String(), nullable=True),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("execution_generation", sa.Integer(), nullable=True),
        sa.Column("stopped_generation", sa.Integer(), nullable=True),
        sa.Column("execution_sessions", sa.JSON(), nullable=True),
        sa.Column("predecessor_token", sa.String(),
                  sa.ForeignKey("automation_operations.token"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "automation_owned_targets",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("operation_token", sa.String(),
                  sa.ForeignKey("automation_operations.token"), nullable=True),
    )
    op.create_index("ix_automation_owned_targets_operation_token", "automation_owned_targets",
                    ["operation_token"])


def downgrade() -> None:
    op.drop_table("automation_owned_targets")
    op.drop_table("automation_operations")
