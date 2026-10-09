"""Configured MCP server keys — the names the CLI's disabledMcpServers list and the scope rules use.

Plugin keys are ``plugin:<plugin-name>:<server>`` with no ``@marketplace`` suffix (spike
docs/superpowers/spikes/2026-10-09-mcp-repo-scope-verification.md, Q2). ``<plugin-name>`` is the plugin's
manifest name (``.claude-plugin/plugin.json``), which ``scan.list_mcp_servers`` exposes as ``pluginName``;
the registry id (``name@marketplace``) can differ, e.g. notion@claude-plugins-official -> "Notion"
(spike addendum, Task 3).
"""
from __future__ import annotations

from server.library import scan


def server_key(entry: dict) -> str:
    if entry["source"] == "user":
        return entry["name"]
    return f"plugin:{entry['pluginName']}:{entry['name']}"


def configured_server_keys() -> list[str]:
    """Blocking (file reads). Callers run it via ``account_store_io`` or ``to_thread``."""
    return sorted({server_key(e) for e in scan.list_mcp_servers()["servers"]})
