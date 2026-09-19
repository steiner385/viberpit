"""Keep verified feature-map dispositions after session cleanup.

Revision ID: 0054_triage_map_resolution
Revises: 0053_decision_proposals

Nullable JSON: init_db reconcile adds this on SQLite and Postgres.
"""
from alembic import op
import sqlalchemy as sa

revision = "0054_triage_map_resolution"
down_revision = "0053_decision_proposals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("triage_proposals")}
    if "map_resolution" not in columns:
        op.add_column("triage_proposals", sa.Column("map_resolution", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("triage_proposals", "map_resolution")
