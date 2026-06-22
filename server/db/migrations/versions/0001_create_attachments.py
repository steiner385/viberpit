"""create attachments table

Revision ID: 0001_create_attachments
Revises:
Create Date: 2026-06-22

First Alembic migration in the repo. SQLite dev relies on init_db()/create_all
(+ column reconcile); this migration is the Postgres/prod path for the new
``attachments`` table introduced by the unified image-attachments feature.

Scope is intentionally just the new table: pre-existing tables on prod were
created out-of-band (create_all), so there is no baseline revision to depend on.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_create_attachments"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "attachments",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("turn_id", sa.String(), nullable=True),
        sa.Column("media_type", sa.String(), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(), nullable=False),
        sa.Column("uploaded_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_attachments_session_id", "attachments", ["session_id"])
    op.create_index("ix_attachments_sha256", "attachments", ["sha256"])


def downgrade() -> None:
    op.drop_index("ix_attachments_sha256", table_name="attachments")
    op.drop_index("ix_attachments_session_id", table_name="attachments")
    op.drop_table("attachments")
