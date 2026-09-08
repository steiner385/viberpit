"""Build the in-process MCP server (named `brand.MCP_SERVER_NAME`) for one CODING session."""
from __future__ import annotations

from claude_agent_sdk import create_sdk_mcp_server
from claude_agent_sdk.types import McpSdkServerConfig

from server.mcp_spec import SERVER_NAME
from server.version import running_git_sha

from .context import SessionToolContext
from .tools import build_session_tools


def build_session_server(ctx: SessionToolContext) -> McpSdkServerConfig:
    return create_sdk_mcp_server(name=SERVER_NAME, version=running_git_sha()[:12] or "dev",
                                 tools=build_session_tools(ctx))
