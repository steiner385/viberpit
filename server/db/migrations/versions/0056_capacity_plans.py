"""Capacity governor plan row (spec 2026-09-19 §4, D5).

Revision ID: 0056_capacity_plans
Revises: 0055_project_priority_rank

Adds the ``capacity_plans`` table: a single row (``id = "current"``) holding the
capacity governor's derived plan, rewritten every tick. This is never the human's
policy — the runner merges this over policy at read time (phase 2). Every column
past the primary key is nullable, so init_db's reconcile can ALTER-ADD a later one
on both dialects.

Reviewable record only: nothing runs ``alembic upgrade`` at deploy time —
``init_db``'s ``create_all`` builds the new table on SQLite and Postgres at next
boot. Guarded so a re-run against a database that already has the table (built by
``create_all`` before this revision was ever applied) is a no-op rather than a
"table already exists" failure.
"""
from alembic import op
import sqlalchemy as sa

revision = "0056_capacity_plans"
down_revision = "0055_project_priority_rank"
branch_labels = None
depends_on = None

TABLE = "capacity_plans"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE not in inspector.get_table_names():
        op.create_table(
            TABLE,
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("computed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("push_level", sa.String(), nullable=True),
            sa.Column("applied", sa.Boolean(), nullable=True),
            sa.Column("fleet_concurrency", sa.Integer(), nullable=True),
            sa.Column("per_product", sa.JSON(), nullable=True),
            sa.Column("pending", sa.JSON(), nullable=True),
            sa.Column("budget", sa.JSON(), nullable=True),
            sa.Column("binding", sa.JSON(), nullable=True),
            sa.Column("reasons", sa.JSON(), nullable=True),
            sa.Column("defects", sa.JSON(), nullable=True),
        )


def downgrade() -> None:
    op.drop_table(TABLE)
