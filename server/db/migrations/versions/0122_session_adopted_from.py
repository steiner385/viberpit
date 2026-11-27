"""sessions.adopted_from — pending marker of a dead-host adoption (spec 002 FR-201..205, #949).

Revision ID: 0122_session_adopted_from
Revises: 0121_webauthn_credentials

Nullable string: the host a session was adopted off, set by the primary's adoption
pass with the restamp and cleared by the adopting host once it has taken the session.
``init_db`` reconcile applies it; this revision is the reviewable record.
"""
import sqlalchemy as sa
from alembic import op

revision = "0122_session_adopted_from"
down_revision = "0121_webauthn_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("adopted_from", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "adopted_from")
