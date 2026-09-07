"""Native display routing: sessions.display_mode ("native" | "virtual" | NULL).

Revision ID: 0031_session_display_mode
Revises: 0030_test_run_slots
Create Date: 2026-09-06

Reviewable record only: nothing runs `alembic upgrade` at deploy time — init_db's
_reconcile_columns ALTER-ADDs the nullable column on both SQLite and Postgres.
"""
from alembic import op
import sqlalchemy as sa

revision = "0031_session_display_mode"
down_revision = "0030_test_run_slots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("display_mode", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "display_mode")
