"""dispatch_outcomes.session_id → sessions.id becomes ON DELETE SET NULL.

Revision ID: 0073_dispatch_outcomes_session_fk_set_null
Revises: 0072_session_suspected_complete_reason

The row is written at dispatch time, before a model-approval hold resolves;
approving the hold DELETEs the placeholder ``sessions`` row to re-INSERT it
under the same id, and the bare FK made Postgres refuse that delete with
ForeignKeyViolationError (the archive purge hit the same wall in #2074).
History outlives its session, so the constraint detaches instead.

Nothing runs `alembic upgrade` on rollout: ``init_db``'s
``_reconcile_fk_on_delete`` rewrites the live constraint at startup from the
model's declared rule. This revision is the reviewable record of that change.
SQLite cannot ALTER a constraint and does not enforce FKs by default, so the
revision is Postgres-only.
"""
from alembic import op
import sqlalchemy as sa

revision = "0073_dispatch_outcomes_session_fk_set_null"
down_revision = "0072_session_suspected_complete_reason"
branch_labels = None
depends_on = None

_TABLE = "dispatch_outcomes"


def _session_fk_name() -> str | None:
    for fk in sa.inspect(op.get_bind()).get_foreign_keys(_TABLE):
        if fk["constrained_columns"] == ["session_id"]:
            return fk["name"]
    return None


def _rewrite(ondelete: str | None) -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    name = _session_fk_name()
    if name is not None:
        op.drop_constraint(name, _TABLE, type_="foreignkey")
    op.create_foreign_key(name or f"{_TABLE}_session_id_fkey", _TABLE, "sessions",
                          ["session_id"], ["id"], ondelete=ondelete)


def upgrade() -> None:
    _rewrite("SET NULL")


def downgrade() -> None:
    _rewrite(None)
