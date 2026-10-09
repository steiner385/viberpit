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

The ``gh_created_at`` bound must stay: a user could register either login later, and
relabelling their rows would make a squatter a trusted App author. Production does not
run Alembic; ``server.db._backfill_app_authors`` applies the same bounded update at
startup. This file covers dev/test databases built through Alembic.
"""
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = '0081_work_item_app_authors'
down_revision = '0080_session_display_fallback_reason'
branch_labels = None
depends_on = None

_APPS = ('github-actions', 'viberpit')
_BOUND = datetime(2026, 10, 10, tzinfo=timezone.utc)


def upgrade() -> None:
    for slug in _APPS:
        op.execute(sa.text(
            "UPDATE work_items SET author = 'app/' || author "
            "WHERE author = :slug AND gh_created_at < :bound"
        ).bindparams(
            sa.bindparam('slug', slug),
            sa.bindparam('bound', _BOUND, type_=sa.DateTime(timezone=True)),
        ))


def downgrade() -> None:
    for slug in _APPS:
        op.execute(sa.text(
            "UPDATE work_items SET author = :slug WHERE author = :app"
        ).bindparams(slug=slug, app=f'app/{slug}'))
