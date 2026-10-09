"""Record GitHub App authors on mirrored work items as ``app/<slug>``.

The GraphQL mirror used to store a Bot author as its bare login, indistinguishable
from a User login, so the trusted-author gate could be squatted by registering a user
of the same name. The mirror now stores ``app/<slug>`` for Bots; this converts the
rows it already wrote.

Only ``github-actions`` and ``viberpit``: both were verified (``gh api users/<login>``
-> 404) to be unregistered as user accounts, so an existing bare row with either name
can only be the App. Other bare logins are NOT converted -- e.g. ``copilot-swe-agent``
IS a registered user, so a bare row of that name may be a person.
"""
import sqlalchemy as sa
from alembic import op

revision = '0080_work_item_app_authors'
down_revision = '0079_mcp_repo_scope'
branch_labels = None
depends_on = None

_APPS = ('github-actions', 'viberpit')


def upgrade() -> None:
    for slug in _APPS:
        op.execute(sa.text(
            "UPDATE work_items SET author = 'app/' || author WHERE author = :slug"
        ).bindparams(slug=slug))


def downgrade() -> None:
    for slug in _APPS:
        op.execute(sa.text(
            "UPDATE work_items SET author = :slug WHERE author = :app"
        ).bindparams(slug=slug, app=f'app/{slug}'))
