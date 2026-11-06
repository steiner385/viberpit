"""D-22 configurable session-branch prefix: app_settings.branch_prefix.

Revision ID: 0110_app_settings_branch_prefix
Revises: 0109_session_branches

NULL = the built-in prefix. Nullable: ``init_db``'s reconcile adds it on both dialects;
this revision is the reviewable record.

Chaining note: chained off 0109_session_branches (the newest revision when written).
If another revision lands first, re-chain ``down_revision`` to the newest one
(``test_alembic_single_head`` enforces one head).
"""
import sqlalchemy as sa
from alembic import op

revision = "0110_app_settings_branch_prefix"
down_revision = "0109_session_branches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("app_settings", sa.Column("branch_prefix", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("app_settings", "branch_prefix")
