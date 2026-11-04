"""Build the in-process `viberpit` MCP server for one assistant session."""
from __future__ import annotations

from collections.abc import Callable

from claude_agent_sdk import create_sdk_mcp_server
from claude_agent_sdk.types import McpSdkServerConfig

from viberpit import brand
from server.version import running_git_sha

from .client import AssistantApi
from .tools import TOOL_SPECS, ToolClass, build_tools


def build_server(api: AssistantApi, *, operator: bool = False,
                 read_only: bool = False,
                 current_scope: Callable[[], str | None] | None = None,
                 ) -> McpSdkServerConfig:
    """The `viberpit` MCP server over `build_tools()`.

    ``current_scope`` is the D-12 side-chat scope source, passed straight to
    `build_tools` (see there): the drawer reads it off its driver's last sent context.

    ``read_only`` serves only ``ToolClass.READ`` tools. The drawer never needs it —
    its mutations are gated by the permission layer — but `ccswitch mcp`
    (src/viberpit/assistant_mcp.py) has no such gate in front of it, so it starts
    read-only and takes `--allow-mutations` to opt in.
    """
    tools = build_tools(api, operator=operator, current_scope=current_scope)
    if read_only:
        tools = [t for t in tools if TOOL_SPECS[t.name].tool_class is ToolClass.READ]
    # `running_git_sha()` is the one version identity the server already exposes
    # (/healthz, deploy gate); an MCP `version` string may be any text.
    return create_sdk_mcp_server(name=brand.MCP_SERVER_NAME, version=running_git_sha()[:12] or "dev",
                                 tools=tools)
