"""create host_heartbeats — fenced per-host daemon heartbeat (spec 002 FR-101)

Revision ID: 0016_host_heartbeats
Revises: 0015_pending_model_reason
Create Date: 2026-09-02

Each host daemon writes its own row (fenced beat via host_heartbeat.beat).
The primary bumps the fence column to declare a host dead (host_heartbeat.declare_dead).
Liveness is derived from beat_at — never stored (FR-102).

Deploy note: nothing runs ``alembic upgrade`` at deploy time — this is a NEW
table, so ``init_db``'s ``create_all`` builds it on every dialect at next boot
(reconcile is only needed for columns added to EXISTING tables). This revision
is the reviewable record of the schema change.
"""
from alembic import op
import sqlalchemy as sa

revision = "0016_host_heartbeats"
down_revision = "0015_pending_model_reason"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "host_heartbeats",
        sa.Column("host", sa.String(), primary_key=True),
        sa.Column("beat_at", sa.DateTime(), nullable=False),
        sa.Column("fence", sa.Integer(), nullable=False),
        sa.Column("epoch", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("host_heartbeats")
