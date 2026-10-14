"""Needs-you hygiene: owner recommendations + needs-you samples (design 2026-10-14 §2).

Revision ID: 0087_needs_you_hygiene
Revises: 0086_work_item_last_reopened_at

Adds two tables. ``owner_recommendations``: a hygiene action a ``cautious`` product
leaves for its owner to apply or dismiss. ``attention_samples``: the hourly needs-you
count per product, for the trend.

Reviewable record only: nothing runs ``alembic upgrade`` at deploy time —
``init_db``'s ``create_all`` builds the new tables on SQLite and Postgres at next
boot. Guarded so a re-run against a database that already has them is a no-op.
"""
from alembic import op
import sqlalchemy as sa

revision = "0087_needs_you_hygiene"
down_revision = "0086_work_item_last_reopened_at"
branch_labels = None
depends_on = None

RECS = "owner_recommendations"
RECS_INDEX = "ix_owner_recommendations_lookup"
SAMPLES = "attention_samples"
SAMPLES_INDEX = "ix_attention_samples_project_sampled"


def _indexes(table: str) -> set[str]:
    return {idx["name"] for idx in sa.inspect(op.get_bind()).get_indexes(table)
            if idx["name"] is not None}


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if RECS not in tables:
        op.create_table(
            RECS,
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("project_id", sa.String(), nullable=False),
            sa.Column("rule", sa.String(), nullable=False),
            sa.Column("target_ref", sa.String(), nullable=False),
            sa.Column("work_item_id", sa.String(), nullable=True),
            sa.Column("action", sa.String(), nullable=False),
            sa.Column("detail", sa.JSON(), nullable=True),
            sa.Column("evidence", sa.Text(), nullable=True),
            sa.Column("status", sa.String(), nullable=False, server_default="pending"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("resolved_by", sa.String(), nullable=True),
        )
    if RECS_INDEX not in _indexes(RECS):
        op.create_index(RECS_INDEX, RECS, ["project_id", "status", "rule", "target_ref"])
    if SAMPLES not in tables:
        op.create_table(
            SAMPLES,
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("project_id", sa.String(), nullable=False),
            sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("needs_you", sa.Integer(), nullable=False),
            sa.Column("waiting", sa.JSON(), nullable=True),
        )
    if SAMPLES_INDEX not in _indexes(SAMPLES):
        op.create_index(SAMPLES_INDEX, SAMPLES, ["project_id", "sampled_at"])


def downgrade() -> None:
    op.drop_index(SAMPLES_INDEX, table_name=SAMPLES)
    op.drop_table(SAMPLES)
    op.drop_index(RECS_INDEX, table_name=RECS)
    op.drop_table(RECS)
