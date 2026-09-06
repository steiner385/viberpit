"""AI assistant v2 9.5: assistant_tokens table for user-scoped external `ccas2:` tokens.

Revision ID: 0028_assistant_tokens
Revises: 0027_assistant_operator_tools
Create Date: 2026-09-06

Reviewable record only: nothing runs `alembic upgrade` at deploy time — init_db's
create_all builds the new table on both SQLite and Postgres at next boot.
"""
from alembic import op
import sqlalchemy as sa

revision = "0028_assistant_tokens"
down_revision = "0027_assistant_operator_tools"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assistant_tokens",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("identity", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("label", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("assistant_tokens")
