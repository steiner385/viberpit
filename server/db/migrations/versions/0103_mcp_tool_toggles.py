"""MCP per-tool toggles (D-16b): tool defaults, session tool overrides, last-seen tools.

Revision ID: 0103_mcp_tool_toggles
Revises: 0102_output_styles

``mcp_server_scope.tool_defaults`` is the Settings default per tool (``{tool: enabled}``);
``sessions.mcp_tool_overrides`` is a session's live override (``{server: {tool:
enabled}}``); ``mcp_server_status.tools`` / ``tools_seen_at`` keep the last tool list a
connected observation carried, so Settings can list tools with no live session. All
nullable: ``init_db``'s reconcile adds them on both dialects; this revision is the
reviewable record.

Chaining note: chained off 0102_output_styles (master's newest revision when written).
If another revision lands first, re-chain ``down_revision`` to the newest one
(``test_alembic_single_head`` enforces one head).
"""
import sqlalchemy as sa
from alembic import op

revision = "0103_mcp_tool_toggles"
down_revision = "0102_output_styles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("mcp_server_scope", sa.Column("tool_defaults", sa.JSON(), nullable=True))
    op.add_column("sessions", sa.Column("mcp_tool_overrides", sa.JSON(), nullable=True))
    op.add_column("mcp_server_status", sa.Column("tools", sa.JSON(), nullable=True))
    op.add_column("mcp_server_status", sa.Column("tools_seen_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("mcp_server_status", "tools_seen_at")
    op.drop_column("mcp_server_status", "tools")
    op.drop_column("sessions", "mcp_tool_overrides")
    op.drop_column("mcp_server_scope", "tool_defaults")
