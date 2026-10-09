"""Configured MCP server keys — the names the CLI's disabledMcpServers list and the scope rules use.

Plugin keys are ``plugin:<plugin-name>:<server>`` with no ``@marketplace`` suffix (spike
docs/superpowers/spikes/2026-10-09-mcp-repo-scope-verification.md, Q2). ``entry["source"]`` is the
registry id from ``scan.enabled_plugin_paths()`` (``name@marketplace``, see ``scan.list_plugins``).
"""
from __future__ import annotations

from server.library import scan


def server_key(entry: dict) -> str:
    if entry["source"] == "user":
        return entry["name"]
    plugin_name = entry["source"].split("@", 1)[0]
    return f"plugin:{plugin_name}:{entry['name']}"


def configured_server_keys() -> list[str]:
    """Blocking (file reads). Callers run it via ``account_store_io`` or ``to_thread``."""
    return sorted({server_key(e) for e in scan.list_mcp_servers()["servers"]})
