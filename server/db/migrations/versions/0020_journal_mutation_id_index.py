"""003 Phase B: index gh_write_journal.mutation_id.

Every planned mutation probes the journal by key each cycle (is it parked? has it
failed before? does it already have a fresh dry-run row?), so the column is read
far more often than written.

NOTE: ``_reconcile_columns`` ALTER-ADDs missing *columns* on every dialect but does
NOT create indexes on an existing table, so the live database needs the index created
by hand (see specs/003-gh-projects-integration/phase-b-runbook.md preconditions):

    CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_gh_write_journal_mutation_id
        ON gh_write_journal (mutation_id);
"""
from alembic import op

revision = "0020_journal_mutation_id_index"
down_revision = "0019_reconciler"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_gh_write_journal_mutation_id", "gh_write_journal", ["mutation_id"])


def downgrade() -> None:
    op.drop_index("ix_gh_write_journal_mutation_id", table_name="gh_write_journal")
