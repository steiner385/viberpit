"""Retry baseline for an initiative whose decomposition the synthesizer gave up on.

Revision ID: 0057_materialize_retry_baseline
Revises: 0056_capacity_plans

Nullable Integer: init_db reconcile adds this on SQLite and Postgres. NULL means
"never retried", which the attempt count treats as a baseline of 0.

Why a baseline column and not a delete of the stale proposals: the SAME total
count is the decompose session's dedupe key (``<slug>:materialize:<n>``), so
lowering it would re-mint a key already spent and hand the loop the previous
attempt's finished session (C4-R15). See #1582.
"""
from alembic import op
import sqlalchemy as sa

revision = "0057_materialize_retry_baseline"
down_revision = "0056_capacity_plans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("initiatives")}
    if "materialize_retry_baseline" not in columns:
        op.add_column("initiatives",
                      sa.Column("materialize_retry_baseline", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("initiatives", "materialize_retry_baseline")
