"""``dispatch_outcomes.dispatch_kind`` (spec 2026-09-18 §3/§5).

Revision ID: 0050_dispatch_kind
Revises: 0049_work_item_holds

What a dispatch was for: ``implement`` (opens a PR) or ``investigate`` (series item
4b — reproduces or correlates, never a bounce). Nullable, no DB-level default: NULL
on rows written before this column existed reads as ``implement`` at the query
layer (``server/pm/outcomes.py:bounces_stmt``).

Nothing runs ``alembic upgrade`` at deploy time: ``init_db``'s reconcile ALTER-ADDs
the nullable column on both dialects. This revision is the reviewable record.
"""
from alembic import op
import sqlalchemy as sa

revision = "0050_dispatch_kind"
down_revision = "0049_work_item_holds"
branch_labels = None
depends_on = None


def _existing() -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {c["name"] for c in inspector.get_columns("dispatch_outcomes")}


def upgrade() -> None:
    if "dispatch_kind" not in _existing():
        op.add_column("dispatch_outcomes", sa.Column("dispatch_kind", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("dispatch_outcomes", "dispatch_kind")
