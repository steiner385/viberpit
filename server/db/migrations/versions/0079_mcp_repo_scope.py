"""Per-repo MCP scope: global defaults, repo rules, and live session overrides."""
import sqlalchemy as sa
from alembic import op

revision = '0079_mcp_repo_scope'
down_revision = '0078_mcp_server_status'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'mcp_server_scope',
        sa.Column('server', sa.String(), primary_key=True),
        sa.Column('default_enabled', sa.Boolean(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_table(
        'mcp_repo_rule',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('repo', sa.String(), nullable=False),
        sa.Column('server', sa.String(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('repo', 'server', name='uq_mcp_repo_rule_key'),
    )
    op.add_column('sessions', sa.Column('mcp_overrides', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('sessions', 'mcp_overrides')
    op.drop_table('mcp_repo_rule')
    op.drop_table('mcp_server_scope')
