"""Build the in-process `ccswitch` MCP server for one assistant session."""
from __future__ import annotations

from claude_agent_sdk import create_sdk_mcp_server
from claude_agent_sdk.types import McpSdkServerConfig

from server.version import running_git_sha

from .client import AssistantApi
from .tools import build_tools


def build_server(api: AssistantApi, *, operator: bool = False) -> McpSdkServerConfig:
    # `running_git_sha()` is the one version identity the server already exposes
    # (/healthz, deploy gate); an MCP `version` string may be any text.
    return create_sdk_mcp_server(name="ccswitch", version=running_git_sha()[:12] or "dev",
                                 tools=build_tools(api, operator=operator))
