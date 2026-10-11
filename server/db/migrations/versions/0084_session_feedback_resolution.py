"""Session Feedback inbox: session_feedback.resolution (how an operator action settled a row, #2936).

Revision ID: 0084_session_feedback_resolution
Revises: 0083_not_needed_material_fingerprint
"""
import sqlalchemy as sa
from alembic import op

revision = "0084_session_feedback_resolution"
down_revision = "0083_not_needed_material_fingerprint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("session_feedback", sa.Column("resolution", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("session_feedback", "resolution")
