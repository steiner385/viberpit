"""Error triage: error_signatures — deduplicated WARNING+ log signatures.

Revision ID: 0034_error_signatures
Revises: 0033_triage_items
Create Date: 2026-09-07

Reviewable record only: nothing runs `alembic upgrade` at deploy time — init_db's
create_all builds the new table on both SQLite and Postgres at next boot, and
_reconcile_columns ALTER-ADDs any later nullable column.
"""
from alembic import op
import sqlalchemy as sa

revision = "0034_error_signatures"
down_revision = "0033_triage_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "error_signatures",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("fingerprint", sa.String(), nullable=True, unique=True),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("level", sa.String(), nullable=True),
        sa.Column("logger", sa.String(), nullable=True),
        sa.Column("event", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("sample_message", sa.Text(), nullable=True),
        sa.Column("sample_exc", sa.Text(), nullable=True),
        sa.Column("sample_context", sa.JSON(), nullable=True),
        sa.Column("sample_at", sa.DateTime(), nullable=True),
        sa.Column("count", sa.Integer(), nullable=True),
        sa.Column("count_since_filed", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("issue_number", sa.Integer(), nullable=True),
        sa.Column("muted_by", sa.String(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("filed_at", sa.DateTime(), nullable=True),
        sa.Column("last_commented_at", sa.DateTime(), nullable=True),
        sa.Column("issue_closed_at", sa.DateTime(), nullable=True),
        sa.Column("muted_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_error_signatures_fingerprint", "error_signatures", ["fingerprint"])
    op.create_index("ix_error_signatures_status", "error_signatures", ["status"])


def downgrade() -> None:
    op.drop_index("ix_error_signatures_status", table_name="error_signatures")
    op.drop_index("ix_error_signatures_fingerprint", table_name="error_signatures")
    op.drop_table("error_signatures")
