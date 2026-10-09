"""Record GitHub App authors on mirrored work items as ``app/<slug>``.

The GraphQL mirror used to store a Bot author as its bare login, indistinguishable
from a User login, so the trusted-author gate could be squatted by registering a user
of the same name. The mirror now stores ``app/<slug>`` for Bots; this converts the
rows it already wrote.

Only ``github-actions`` and ``viberpit``: both were verified (``gh api users/<login>``
-> 404) on 2026-10-09 to be unregistered as user accounts, so an existing bare row
with either name created before that check can only be the App. Other bare logins are
NOT converted -- e.g. ``copilot-swe-agent`` IS a registered user, so a bare row of
that name may be a person.

ONE-SHOT by design, and it must stay that way: GitHub users can RENAME. Someone who
opened an issue long ago can later rename to the unregistered ``viberpit`` /
``github-actions`` login; the next sync stores that bare login on an OLD row, and a
repeated relabel would make them a trusted App author. Alembic runs this once per
database; it also creates the ``data_backfills`` marker table and records the
``work_item_app_authors`` marker so the startup twin
(``server.db._backfill_app_authors``, the path production actually runs -- it never
runs Alembic) skips. The UPDATE is further bounded to rows created before 2026-10-10
and last written by the mirror before this run (``last_synced_at``).
"""
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = '0082_work_item_app_authors'
down_revision = '0081_drop_test_run_slots'
branch_labels = None
depends_on = None

_APPS = ('github-actions', 'viberpit')
_BOUND = datetime(2026, 10, 10, tzinfo=timezone.utc)
_MARKER = 'work_item_app_authors'


def upgrade() -> None:
    marker = op.create_table(
        'data_backfills',
        sa.Column('name', sa.String(), primary_key=True),
        sa.Column('applied_at', sa.DateTime(timezone=True), nullable=False),
    )
    now = datetime.now(timezone.utc)
    stamp = sa.DateTime(timezone=True)
    for slug in _APPS:
        op.execute(sa.text(
            "UPDATE work_items SET author = 'app/' || author "
            "WHERE author = :slug AND gh_created_at < :bound AND last_synced_at < :now"
        ).bindparams(
            sa.bindparam('slug', slug),
            sa.bindparam('bound', _BOUND, type_=stamp),
            sa.bindparam('now', now, type_=stamp),
        ))
    op.bulk_insert(marker, [{'name': _MARKER, 'applied_at': now}])


def downgrade() -> None:
    for slug in _APPS:
        op.execute(sa.text(
            "UPDATE work_items SET author = :slug WHERE author = :app"
        ).bindparams(slug=slug, app=f'app/{slug}'))
    op.drop_table('data_backfills')
