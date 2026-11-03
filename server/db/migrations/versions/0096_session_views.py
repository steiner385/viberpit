"""Per-viewer session read state (D-10): the session_views table.

Revision ID: 0096_session_views
Revises: 0095_session_rewinds

One row per (viewer identity, session): when that viewer last looked at it. A session
is unread for a viewer iff the row exists and ``sessions.last_turn_at`` is later than
``viewed_at``; no row = read. ``create_all`` makes the table on both dialects; this
revision is the reviewable record.
"""
import sqlalchemy as sa
from alembic import op

revision = "0096_session_views"
down_revision = "0095_session_rewinds"
branch_labels = None
depends_on = None

TABLE = "session_views"
IDENTITY_INDEX = "ix_session_views_identity"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("identity", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("viewed_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("identity", "session_id"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
    )
    op.create_index(IDENTITY_INDEX, TABLE, ["identity"])


def downgrade() -> None:
    op.drop_index(IDENTITY_INDEX, table_name=TABLE)
    op.drop_table(TABLE)
