"""Typed holds on work items (spec 2026-09-18 §3).

Revision ID: 0049_work_item_holds
Revises: 0048_artifact_auto_merge_armed

Adds ``work_items.hold_kind / hold_reason / hold_by / hold_set_at /
hold_fingerprint`` (all nullable) and converts the pre-ladder
``triage_state = 'needs_human'`` rows into owner_action holds by ``migration``.

Nothing runs ``alembic upgrade`` at deploy time: ``init_db``'s reconcile ALTER-ADDs
the nullable columns, and ``scripts/convert_legacy_needs_human.py`` runs the same
conversion statements (``server.pm.holds.LEGACY_CONVERSION_SQL``) once on the live
DB. This revision is the reviewable record of both.
"""
from alembic import op
import sqlalchemy as sa

from server.pm.holds import LEGACY_CONVERSION_SQL

revision = "0049_work_item_holds"
down_revision = "0048_artifact_auto_merge_armed"
branch_labels = None
depends_on = None

_COLUMNS = (
    sa.Column("hold_kind", sa.String(), nullable=True),
    sa.Column("hold_reason", sa.String(), nullable=True),
    sa.Column("hold_by", sa.String(), nullable=True),
    sa.Column("hold_set_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("hold_fingerprint", sa.String(), nullable=True),
)


def _existing() -> set[str]:
    bind = op.get_bind()
    return {c["name"] for c in sa.inspect(bind).get_columns("work_items")}


def upgrade() -> None:
    have = _existing()
    for col in _COLUMNS:
        if col.name not in have:
            op.add_column("work_items", col)
    if "hold_kind" not in have:
        op.create_index("ix_work_items_hold_kind", "work_items", ["hold_kind"])
    for stmt in LEGACY_CONVERSION_SQL:
        op.execute(sa.text(stmt))


def downgrade() -> None:
    op.drop_index("ix_work_items_hold_kind", table_name="work_items")
    for col in reversed(_COLUMNS):
        op.drop_column("work_items", col.name)
