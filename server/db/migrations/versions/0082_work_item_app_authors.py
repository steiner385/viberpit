"""Re-mirror GitHub authors so Apps are recorded as ``app/<slug>``.

The GraphQL mirror used to store a Bot author as its bare login, indistinguishable
from a User login, so the trusted-author gate could be squatted by registering a user
of the same name. The mirror now stores ``app/<slug>`` for Bots (it reads
``__typename``); this fixes the rows the OLD sync already wrote.

It does NOT relabel bare logins as ``app/<slug>``: a stored login is a stale identity --
it records what the account was called when the row was synced, not whether it was the
App. Anyone who registered or renamed to ``viberpit``/``github-actions`` before this ran
has bare rows indistinguishable from the App's. Instead the bare rows are CLEARED
(``author`` NULL: untrusted, fail closed) and every repo's mirror backfill is reset, so
the new sync re-reads every author and GitHub itself says which rows are Bots.

ONE-SHOT by design: Alembic runs this once per database; it also creates the
``data_backfills`` marker table and records the ``work_item_app_authors`` marker so the
startup twin (``server.db._backfill_app_authors``, the path production actually runs --
it never runs Alembic) skips. After it, a bare ``viberpit`` row can only come from the
new sync, i.e. a USER of that name, and must be left alone.
"""
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = '0082_work_item_app_authors'
down_revision = '0081_drop_test_run_slots'
branch_labels = None
depends_on = None

_APPS = ('github-actions', 'viberpit')
_MARKER = 'work_item_app_authors'


def upgrade() -> None:
    marker = op.create_table(
        'data_backfills',
        sa.Column('name', sa.String(), primary_key=True),
        sa.Column('applied_at', sa.DateTime(timezone=True), nullable=False),
    )
    for slug in _APPS:
        op.execute(sa.text(
            "UPDATE work_items SET author = NULL WHERE author = :slug"
        ).bindparams(sa.bindparam('slug', slug)))
    op.execute(sa.text(
        "UPDATE project_repos SET backfill_done = :done, backfill_cursor = NULL, "
        "backfill_phase = 'issues'"
    ).bindparams(sa.bindparam('done', False, type_=sa.Boolean())))
    op.bulk_insert(marker, [{'name': _MARKER, 'applied_at': datetime.now(timezone.utc)}])


def downgrade() -> None:
    # Cleared authors are not restored: re-writing a bare App-looking login is exactly
    # the stale identity this migration removes. The re-mirror refills them.
    op.drop_table('data_backfills')
