"""Converge automation dedupe keys off the pre-rebrand product slug (#1369).

Revision ID: 0061_brand_automation_keys
Revises: 0060_backlog_groups_degraded

DATA-only, no schema change — which is exactly why this revision cannot be the
thing that runs. `init_db`'s `_reconcile_columns` applies columns, not UPDATEs, and
nothing runs `alembic upgrade` at deploy time, so this file is the reviewable record
and `scripts/rename_automation_keys_brand.py` is what an operator actually runs in the
maintenance window. Both drive `server/db/brand_keys.py`, so the record and the run
cannot describe different statements.

Irreversible on purpose. `downgrade()` would have to re-prefix keys the emitter no
longer composes, re-creating exactly the split identity this removes, and it cannot
restore an `automation_target_state` row the collision delete reaped.
"""
from alembic import op
import sqlalchemy as sa

from server.db.brand_keys import (
    KEY_COLUMNS,
    delete_collisions_sql,
    update_sql,
)

revision = "0061_brand_automation_keys"
down_revision = "0060_backlog_groups_degraded"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    for col in KEY_COLUMNS:
        # A revision run against a partially built database must not abort the chain.
        if col.table not in tables:
            continue
        collisions = delete_collisions_sql(col)
        if collisions:
            op.execute(collisions)
        op.execute(update_sql(col))


def downgrade() -> None:
    raise NotImplementedError(
        "0061 converges automation dedupe keys in place; there is no safe reverse "
        "(see the module docstring)")
