"""Decision proposals (spec 2026-09-18 §6).

Revision ID: 0053_decision_proposals
Revises: 0052_pr_resolution_note

Adds the ``decision_proposals`` table: the options a coding session drafted for a
product decision it could not take, the one it recommended, the rationale, and the
key a person eventually chose. Every column past the primary key is nullable, so a
later addition can ride ``init_db``'s reconcile on both dialects.

Reviewable record only: nothing runs ``alembic upgrade`` at deploy time —
``init_db``'s ``create_all`` builds the new table on SQLite and Postgres at next
boot. Guarded so a re-run against a database that already has the table (built by
``create_all`` before this revision was ever applied) is a no-op rather than a
"table already exists" failure.
"""
from alembic import op
import sqlalchemy as sa

revision = "0053_decision_proposals"
down_revision = "0052_pr_resolution_note"
branch_labels = None
depends_on = None

TABLE = "decision_proposals"
WORK_ITEM_INDEX = "ix_decision_proposals_work_item_id"
STATUS_INDEX = "ix_decision_proposals_status"
#: Partial UNIQUE index: at most one ``pending`` proposal per work item, enforced
#: by the database rather than by the service's SELECT alone (two concurrent
#: proposers can both pass that check). Resolved rows are outside it — an issue may
#: be decided many times over its life. Both dialects accept a partial index, so
#: the predicate is spelled for each.
PENDING_INDEX = "ux_decision_proposals_pending"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE not in inspector.get_table_names():
        op.create_table(
            TABLE,
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("work_item_id", sa.String(), sa.ForeignKey("work_items.id"),
                      nullable=True),
            sa.Column("session_id", sa.String(), nullable=True),
            sa.Column("options", sa.JSON(), nullable=True),
            sa.Column("recommended_key", sa.String(), nullable=True),
            sa.Column("rationale", sa.String(), nullable=True),
            sa.Column("chosen_key", sa.String(), nullable=True),
            sa.Column("status", sa.String(), nullable=True),
            sa.Column("proposed_by", sa.String(), nullable=True),
            sa.Column("resolved_by", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        )
    indexes = {idx["name"] for idx in sa.inspect(op.get_bind()).get_indexes(TABLE)
               if idx["name"] is not None}
    if WORK_ITEM_INDEX not in indexes:
        op.create_index(WORK_ITEM_INDEX, TABLE, ["work_item_id"])
    if STATUS_INDEX not in indexes:
        op.create_index(STATUS_INDEX, TABLE, ["status"])
    if PENDING_INDEX not in indexes:
        op.create_index(PENDING_INDEX, TABLE, ["work_item_id"], unique=True,
                        sqlite_where=sa.text("status = 'pending'"),
                        postgresql_where=sa.text("status = 'pending'"))


def downgrade() -> None:
    op.drop_index(PENDING_INDEX, table_name=TABLE)
    op.drop_index(STATUS_INDEX, table_name=TABLE)
    op.drop_index(WORK_ITEM_INDEX, table_name=TABLE)
    op.drop_table(TABLE)
