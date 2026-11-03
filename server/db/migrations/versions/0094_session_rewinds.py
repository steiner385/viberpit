"""Rewind to here (D-9): the session_rewinds table.

Revision ID: 0094_session_rewinds
Revises: 0093_session_fork_resume

One row per rewind of a session to an earlier prompt: the target, the transcript's
pre-rewind byte length and the undo backup's dir, so the latest rewind can be undone
until the next prompt. ``create_all`` makes the table on both dialects; this revision
is the reviewable record.
"""
import sqlalchemy as sa
from alembic import op

revision = "0094_session_rewinds"
down_revision = "0093_session_fork_resume"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_rewinds",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("to_prompt_uuid", sa.String(), nullable=True),
        sa.Column("to_turn_id", sa.String(), nullable=True),
        sa.Column("jsonl_path", sa.String(), nullable=True),
        sa.Column("jsonl_len", sa.BigInteger(), nullable=True),
        sa.Column("backup_dir", sa.String(), nullable=True),
        sa.Column("files_restored", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("undone_at", sa.DateTime(), nullable=True),
        sa.Column("expired_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_session_rewinds_session_id", "session_rewinds", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_session_rewinds_session_id", table_name="session_rewinds")
    op.drop_table("session_rewinds")
