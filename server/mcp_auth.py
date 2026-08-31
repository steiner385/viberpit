"""McpAuthManager — one-at-a-time MCP-server auth via `claude mcp login`.

Mirrors server/reauth.py's state-machine shape. Owns a live PTY subprocess
that prints an authorize URL (surfaced to the UI) then blocks on stdin for the
user-pasted redirect URL. On success, merges the new mcpOAuth into the shared
store and clears stale per-account needs-auth caches.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Terminal states — polling stops here.
TERMINAL = ("success", "error", "cancelled")


@dataclass
class McpAuthState:
    state: str = "idle"          # idle|starting|awaiting_redirect|exchanging|success|error|cancelled
    server: str | None = None
    message: str | None = None
    auth_url: str | None = None
    accounts: list[str] = field(default_factory=list)  # accounts whose badge this run clears
    started_at: float | None = None

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "server": self.server,
            "message": self.message,
            "authUrl": self.auth_url,
            "accounts": list(self.accounts),
            "startedAt": self.started_at,
        }

    @staticmethod
    def from_dict(d: dict) -> "McpAuthState":
        return McpAuthState(
            state=d.get("state", "idle"),
            server=d.get("server"),
            message=d.get("message"),
            auth_url=d.get("authUrl"),
            accounts=list(d.get("accounts", [])),
            started_at=d.get("startedAt"),
        )
