"""Sanitized MCP server status — the one vocabulary every surface uses (spec 2026-10-09).

The SDK's McpServerStatus carries the server ``config`` (headers, env: secrets). Nothing
downstream ever sees it: this module is the only place a raw status dict is read."""
from __future__ import annotations

import re
from dataclasses import dataclass

from server.mcp_spec import SERVER_NAME

ERROR_MAX = 500
_QUERY = re.compile(r"(https?://[^\s?#]+)\?[^\s#]*")


@dataclass(frozen=True, slots=True)
class McpServerState:
    name: str
    status: str
    error: str | None
    tool_count: int | None
    scope: str | None

    def to_wire(self) -> dict:
        return {"name": self.name, "status": self.status, "error": self.error,
                "toolCount": self.tool_count, "scope": self.scope}


def redact_error(text: str | None) -> str | None:
    if not isinstance(text, str) or not text:
        return None
    return _QUERY.sub(r"\1?<redacted>", text)[:ERROR_MAX]


def sanitize_statuses(raw: object) -> list[McpServerState]:
    entries = raw.get("mcpServers") if isinstance(raw, dict) else None
    out: list[McpServerState] = []
    for s in entries if isinstance(entries, list) else []:
        if not isinstance(s, dict):
            continue
        name, status = s.get("name"), s.get("status")
        if not isinstance(name, str) or not isinstance(status, str):
            continue
        if name == SERVER_NAME:
            continue
        scope = s.get("scope") if isinstance(s.get("scope"), str) else None
        tools = s.get("tools")
        out.append(McpServerState(name=name, status=status, error=redact_error(s.get("error")),
                                  tool_count=len(tools) if isinstance(tools, list) else None,
                                  scope=scope))
    return out
