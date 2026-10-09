"""MCP live status per (account, host, server); create_all installs the table at boot."""
import sqlalchemy as sa
from alembic import op

revision = '0078_mcp_server_status'
down_revision = '0077_personal_quick_replies'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'mcp_server_status',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('account', sa.String(), nullable=False),
        sa.Column('host', sa.String(), nullable=False),
        sa.Column('server', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('observed_at', sa.DateTime(), nullable=False),
        sa.Column('error', sa.String(), nullable=True),
        sa.Column('tool_count', sa.Integer(), nullable=True),
        sa.Column('scope', sa.String(), nullable=True),
        sa.Column('session_id', sa.String(), nullable=True),
        sa.UniqueConstraint('account', 'host', 'server', name='uq_mcp_server_status_key'),
    )


def downgrade() -> None:
    op.drop_table('mcp_server_status')
