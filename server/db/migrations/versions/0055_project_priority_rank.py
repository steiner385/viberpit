"""Per-product priority rank for the capacity governor.

Revision ID: 0055_project_priority_rank
Revises: 0054_triage_map_resolution

Nullable Integer: init_db reconcile adds this on SQLite and Postgres.
"""
from alembic import op
import sqlalchemy as sa

revision = "0055_project_priority_rank"
down_revision = "0054_triage_map_resolution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("projects")}
    if "priority_rank" not in columns:
        op.add_column("projects", sa.Column("priority_rank", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "priority_rank")
