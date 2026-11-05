"""Move a session to another fleet host (D-17): sessions.moving_to, move_started_at,
moved_from, moved_at.

Revision ID: 0106_session_move
Revises: 0105_release_milestones

``moving_to`` is the compare-and-set claim of an in-flight move (NULL in every terminal
state) and ``move_started_at`` its claim time, so a saga that died with its edge is
reconciled once stale. ``moved_from`` / ``moved_at`` record the last completed restamp.
All nullable: ``init_db``'s reconcile adds them on both dialects; this revision is the
reviewable record.

Chaining note: chained off 0105_release_milestones (origin/master's newest revision when
written). If another revision lands first, re-chain ``down_revision`` to the newest one
(``test_alembic_single_head`` enforces one head).
"""
import sqlalchemy as sa
from alembic import op

revision = "0106_session_move"
down_revision = "0105_release_milestones"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("moving_to", sa.String(), nullable=True))
    op.add_column("sessions", sa.Column("move_started_at", sa.DateTime(), nullable=True))
    op.add_column("sessions", sa.Column("moved_from", sa.String(), nullable=True))
    op.add_column("sessions", sa.Column("moved_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "moved_at")
    op.drop_column("sessions", "moved_from")
    op.drop_column("sessions", "move_started_at")
    op.drop_column("sessions", "moving_to")
