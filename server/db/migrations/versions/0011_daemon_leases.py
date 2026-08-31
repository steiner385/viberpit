"""create daemon_leases — DB-side primary-daemon lease (#852)

Revision ID: 0011_daemon_leases
Revises: 0010_app_sessions_composite_index
Create Date: 2026-08-30

``CCSWITCH_DAEMON_ROLE`` is per-host env config: nothing structural stops two
hosts both being configured ``primary``, and two primaries double-drive every
fleet-global automation with no detection (the 2026-08-30 incident shape).
The ``daemon_leases`` table turns that runbook invariant into a checked one: a
primary daemon must acquire the ``name='primary'`` row to finish ``start()``,
renews it on a heartbeat (TTL/5), and a second primary fails its boot loudly.
``fence`` increments on every (re-)acquisition; renew/release are conditional
on (holder, fence), so a fenced-over daemon cannot resurrect the lease.

Deploy note: nothing runs ``alembic upgrade`` at deploy time — this table is a
NEW table, so ``init_db``'s ``create_all`` builds it on every dialect at next
boot (reconcile is only needed for columns added to EXISTING tables). This
revision is the reviewable record of the schema change.
"""
from alembic import op
import sqlalchemy as sa

revision = "0011_daemon_leases"
down_revision = "0010_app_sessions_composite_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "daemon_leases",
        sa.Column("name", sa.String(), primary_key=True),
        sa.Column("holder", sa.String(), nullable=False),
        sa.Column("fence", sa.BigInteger(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(), nullable=False),
        sa.Column("renewed_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("daemon_leases")
