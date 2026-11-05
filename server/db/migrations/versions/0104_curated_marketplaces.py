"""Curated plugin marketplaces (D-16c): app_settings.curated_marketplaces.

Revision ID: 0104_curated_marketplaces
Revises: 0103_mcp_tool_toggles

A JSON object ``{name: normalized_source}`` of the marketplaces the owner confirmed
(server/library/curated.py); NULL means none. Nullable: ``init_db``'s reconcile adds it on
both dialects; this revision is the reviewable record.

Chaining note: chained off 0103_mcp_tool_toggles (master's newest revision when written).
If another revision lands first, re-chain ``down_revision`` to the newest one
(``test_alembic_single_head`` enforces one head).
"""
import sqlalchemy as sa
from alembic import op

revision = "0104_curated_marketplaces"
down_revision = "0103_mcp_tool_toggles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("app_settings",
                  sa.Column("curated_marketplaces", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("app_settings", "curated_marketplaces")
