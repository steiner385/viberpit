"""Scoped always-allow permission profiles + per-rule stats (spec 2026-10-11 §2).

``permission_profiles.scope`` / ``scope_key`` mark the auto-managed always-allow profiles
(NULL for user-authored ones); ``permission_rule_stats`` holds who made each standing rule,
when, and how often it fired. The reconcile path adds the nullable columns and ``create_all``
builds the table, so this revision is the reviewable record.
"""
import sqlalchemy as sa
from alembic import op

revision = '0083_permission_rule_scopes'
down_revision = '0082_work_item_app_authors'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('permission_profiles', sa.Column('scope', sa.String(), nullable=True))
    op.add_column('permission_profiles', sa.Column('scope_key', sa.String(), nullable=True))
    op.create_table(
        'permission_rule_stats',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('profile_id', sa.String(), nullable=False),
        sa.Column('pattern', sa.String(), nullable=False),
        sa.Column('created_by', sa.String(), nullable=False),
        sa.Column('created_via', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('hit_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_hit_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('profile_id', 'pattern', name='uq_permission_rule_stats_profile_pattern'),
    )
    op.create_index('ix_permission_rule_stats_profile_id', 'permission_rule_stats', ['profile_id'])


def downgrade() -> None:
    op.drop_index('ix_permission_rule_stats_profile_id', table_name='permission_rule_stats')
    op.drop_table('permission_rule_stats')
    op.drop_column('permission_profiles', 'scope_key')
    op.drop_column('permission_profiles', 'scope')
