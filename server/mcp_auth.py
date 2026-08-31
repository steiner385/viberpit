"""McpAuthManager — one-at-a-time MCP-server auth via `claude mcp login`.

Mirrors server/reauth.py's state-machine shape. Owns a live PTY subprocess
that prints an authorize URL (surfaced to the UI) then blocks on stdin for the
user-pasted redirect URL. On success, merges the new mcpOAuth into the shared
store and clears stale per-account needs-auth caches.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ccswitch import paths
from ccswitch.creds import read_json, write_json_secure
from server.library.scan import read_json_safe

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


def merge_mcp_oauth_from_cfg(cfg_dir: Path) -> int:
    """Merge <cfg_dir>/.credentials.json's mcpOAuth into the shared MCP store."""
    cred = read_json_safe(cfg_dir / ".credentials.json") or {}
    mcp = cred.get("mcpOAuth")
    if not isinstance(mcp, dict) or not mcp:
        return 0
    shared_path = paths.shared_mcp_path()
    existing = read_json_safe(shared_path) or {}
    existing.update(mcp)
    write_json_secure(shared_path, existing)
    return len(existing)


def _bare(key: str) -> str:
    return key.rsplit(":", 1)[-1] if key.startswith("plugin:") else key


def clear_needs_auth_cache(server: str) -> list[str]:
    """Drop `server`'s entry from every account's mcp-needs-auth-cache.json."""
    cleared: list[str] = []
    root = paths.cfg_root()
    if not root.is_dir():
        return cleared
    for acct_dir in sorted(root.iterdir()):
        cache_path = acct_dir / "mcp-needs-auth-cache.json"
        cache = read_json_safe(cache_path)
        if not isinstance(cache, dict):
            continue
        keys = [k for k in cache if _bare(k) == server]
        if not keys:
            continue
        for k in keys:
            cache.pop(k, None)
        write_json_secure(cache_path, cache)
        cleared.append(acct_dir.name)
    return cleared
