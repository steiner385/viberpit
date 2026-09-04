"""model-approval gate columns — sessions.pending_model* + app_settings policy

Revision ID: 0012_model_approval_columns
Revises: 0011_daemon_leases
Create Date: 2026-08-31

Reviewable record only: nothing runs `alembic upgrade` at deploy time — init_db's
_reconcile_columns ALTER-ADDs these nullable columns on both SQLite and Postgres
at next boot. All columns are nullable, so no default-synthesis / drift issue.
"""
from alembic import op
import sqlalchemy as sa

revision = "0012_model_approval_columns"
down_revision = "0011_daemon_leases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("pending_model", sa.String(), nullable=True))
    op.add_column("sessions", sa.Column("pending_model_requested_by", sa.String(), nullable=True))
    op.add_column("sessions", sa.Column("pending_model_requested_at", sa.DateTime(), nullable=True))
    op.add_column("app_settings", sa.Column("model_approval_action", sa.String(), nullable=True))
    op.add_column("app_settings", sa.Column("model_approval_denylist", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("app_settings", "model_approval_denylist")
    op.drop_column("app_settings", "model_approval_action")
    op.drop_column("sessions", "pending_model_requested_at")
    op.drop_column("sessions", "pending_model_requested_by")
    op.drop_column("sessions", "pending_model")
