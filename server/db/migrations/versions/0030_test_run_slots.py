"""create test_run_slots — verification-phase test-run slot cap (#822)

Revision ID: 0030_test_run_slots
Revises: 0029_notification_rule_scope_session
Create Date: 2026-09-06

Deploy note: nothing runs ``alembic upgrade`` at deploy time — this is a NEW
table, so ``init_db``'s ``create_all`` builds it on every dialect at next boot
(reconcile is only needed for columns added to EXISTING tables). This revision
is the reviewable record of the schema change.
"""
from alembic import op
import sqlalchemy as sa

revision = "0030_test_run_slots"
down_revision = "0029_notification_rule_scope_session"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "test_run_slots",
        sa.Column("session_id", sa.String(), primary_key=True),
        sa.Column("host", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("test_run_slots")
