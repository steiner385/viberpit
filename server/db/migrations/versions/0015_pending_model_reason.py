"""self-directed model switching — sessions.pending_model_reason

Revision ID: 0015_pending_model_reason
Revises: 0014_pm_product_layer
Create Date: 2026-09-02

Reviewable record only: nothing runs `alembic upgrade` at deploy time — init_db's
_reconcile_columns ALTER-ADDs this nullable column on both SQLite and Postgres at
next boot. Nullable, so no default-synthesis / drift issue.
"""
from alembic import op
import sqlalchemy as sa

revision = "0015_pending_model_reason"
down_revision = "0014_pm_product_layer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("pending_model_reason", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "pending_model_reason")
