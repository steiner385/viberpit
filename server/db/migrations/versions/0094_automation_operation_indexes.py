"""Index automation_operations' lineage-walk columns (#3230).

Revision ID: 0094_automation_operation_indexes
Revises: 0093_session_fork_resume

``ownership._naming`` filters on ``session_id`` / ``reserved_session_id`` once per
ancestor level of every lineage walk, and ``_finished`` on ``predecessor_token`` once
per transfer hop; ``lineage_unowned`` runs twice per fenced driver start. None was
indexed. Reconcile never creates an index on an existing table, so on the live
Postgres DB these are created by hand (``deploy/README.md``, "Migrations never run
automatically"); fresh databases get them from the model (``index=True``).
"""
from alembic import op

revision = "0094_automation_operation_indexes"
down_revision = "0093_session_fork_resume"
branch_labels = None
depends_on = None

TABLE = "automation_operations"
INDEXES = {
    "ix_automation_operations_predecessor_token": "predecessor_token",
    "ix_automation_operations_session_id": "session_id",
    "ix_automation_operations_reserved_session_id": "reserved_session_id",
}


def upgrade() -> None:
    for name, column in INDEXES.items():
        op.create_index(name, TABLE, [column])


def downgrade() -> None:
    for name in INDEXES:
        op.drop_index(name, table_name=TABLE)
