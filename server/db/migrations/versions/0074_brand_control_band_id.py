"""Converge the SDLC control-band id off the pre-rebrand product slug (#1365).

Revision ID: 0074_brand_control_band_id
Revises: 0073_dispatch_outcomes_session_fk_set_null

``server/sdlc/bands.yaml`` names its PR-cycle-time band ``<product-slug>-pr-cycle-time``,
and the monitor persists that id into every triage row it files: as ``metric``, as the
leading half of the row's unique ``key`` (``<metric>:<rule>:<side>``) and as the first
word of its ``summary``. Flipping the id in the yaml alone would leave the incident
already filed — and already adopted by Intake — unmatched, so the next tick would file
the same breach again as a fresh, open row.

DATA-only, no schema change, and therefore — exactly like ``0061_brand_automation_keys``
— not the thing that runs: ``init_db`` applies columns, not UPDATEs, and nothing runs
``alembic upgrade`` at deploy time. This file is the reviewable record;
``scripts/rename_automation_keys_brand.py`` is what an operator actually runs. Both
drive ``server/db/brand_keys.BAND_ID_COLUMNS``, so the record and the run cannot
describe different statements.

Irreversible on purpose: a ``downgrade()`` would re-split the incident's identity (the
yaml composes the new id now) and cannot restore a re-detection row the collision
delete reaped.
"""
from alembic import op
import sqlalchemy as sa

from server.db.brand_keys import (
    BAND_ID_COLUMNS,
    delete_collisions_sql,
    update_sql,
)

revision = "0074_brand_control_band_id"
down_revision = "0073_dispatch_outcomes_session_fk_set_null"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    for col in BAND_ID_COLUMNS:
        # A revision run against a partially built database must not abort the chain.
        if col.table not in tables:
            continue
        collisions = delete_collisions_sql(col)
        if collisions:
            op.execute(collisions)
        op.execute(update_sql(col))


def downgrade() -> None:
    raise NotImplementedError(
        "0074 converges the control-band id in place; there is no safe reverse "
        "(see the module docstring)")
