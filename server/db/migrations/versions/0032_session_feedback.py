"""Session tools Phase 1: session_feedback table for `report_feedback` rows.

Revision ID: 0032_session_feedback
Revises: 0031_session_display_mode
Create Date: 2026-09-06

Reviewable record only: nothing runs `alembic upgrade` at deploy time — init_db's
create_all builds the new table on both SQLite and Postgres at next boot.
"""
from alembic import op
import sqlalchemy as sa

revision = "0032_session_feedback"
down_revision = "0031_session_display_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_feedback",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("session_id", sa.String(), nullable=True, index=True),
        sa.Column("fingerprint", sa.String(), nullable=True, index=True),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("impact", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("context", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("issue_number", sa.Integer(), nullable=True),
        sa.Column("reported_at", sa.DateTime(), nullable=True),
        sa.Column("filed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("session_id", "fingerprint", name="uq_session_feedback_session_fp"),
    )


def downgrade() -> None:
    op.drop_table("session_feedback")
