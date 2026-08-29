"""add SDLC artifact-chain spine: initiatives + initiative_artifacts

Revision ID: 0009_sdlc_initiatives
Revises: 0008_ci_test_result_repo_created_ts_index
Create Date: 2026-08-29

Adds the two tables that back the AI-native SDLC lifecycle (#795 Phase 1 /
#796): ``initiatives`` (one unit of work moving intake → … → done) and
``initiative_artifacts`` (its ordered intent/spec/plan document chain).

SQLite dev relies on init_db()/create_all + _reconcile_columns; this migration
is the reviewable Postgres/prod record. Nothing runs `alembic upgrade` at
deploy — on a live Postgres DB reconcile ALTER-ADDs the columns, and because
these are brand-new tables create_all builds them fully-formed. Every non-key
column is nullable so reconcile can add them on any dialect.

No new state table for the initiative state machine: per-slug retry/loop-guard
counters reuse the existing automation_target_state table.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009_sdlc_initiatives"
down_revision: Union[str, None] = "0008_ci_test_result_repo_created_ts_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- initiatives table ---
    op.create_table(
        "initiatives",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=True),
        sa.Column("repo", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("stage", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("origin", sa.String(), nullable=True),
        sa.Column("origin_ref", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_initiatives_slug", "initiatives", ["slug"], unique=True)

    # --- initiative_artifacts table ---
    op.create_table(
        "initiative_artifacts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("initiative_id", sa.String(), nullable=True),
        sa.Column("kind", sa.String(), nullable=True),
        sa.Column("repo_path", sa.String(), nullable=True),
        sa.Column("commit_sha", sa.String(), nullable=True),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("pr_url", sa.String(), nullable=True),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["initiative_id"], ["initiatives.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_initiative_artifacts_initiative_id",
        "initiative_artifacts", ["initiative_id"])


def downgrade() -> None:
    op.drop_index(
        "ix_initiative_artifacts_initiative_id", table_name="initiative_artifacts")
    op.drop_table("initiative_artifacts")
    op.drop_index("ix_initiatives_slug", table_name="initiatives")
    op.drop_table("initiatives")
