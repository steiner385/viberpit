"""Curated plugin marketplaces (D-16c): app_settings.curated_marketplaces.

Revision ID: 0104_curated_marketplaces
Revises: 0103_mcp_tool_toggles

A JSON list of the marketplace names the owner curated; NULL until first read seeds it
from the host's ``known_marketplaces.json``. Nullable: ``init_db``'s reconcile adds it on
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
