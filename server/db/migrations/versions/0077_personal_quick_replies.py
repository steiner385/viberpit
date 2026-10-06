"""Personal bounded reply corpus and curation cache; create_all installs the table."""
import sqlalchemy as sa
from alembic import op

revision = '0077_personal_quick_replies'
down_revision = '0076_sessions_background_work'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'quick_reply_profiles',
        sa.Column('identity', sa.String(320), primary_key=True),
        sa.Column('samples', sa.JSON(), nullable=False),
        sa.Column('replies', sa.JSON(), nullable=False),
        sa.Column('attempted_at', sa.Float(), nullable=False),
        sa.Column('curated_at', sa.Float(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('quick_reply_profiles')
