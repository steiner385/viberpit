"""Compute the CLI disabledMcpServers list for a session build (spec repo-scope section 3)."""
from __future__ import annotations

import logging

from server.config import get_settings
from server.mcp_scope import store
from server.mcp_scope.resolve import effective_disabled
from server.mcp_scope.servers import configured_server_keys
from server.sessions.account_auth import account_store_io

log = logging.getLogger(__name__)


async def disabled_for_session(*, repo: str | None, session_id: str | None) -> list[str] | None:
    """The sorted server keys to disable for this session, or None to write no list.

    None means "leave the CLI's list alone": the kill switch is off or the rules could not
    be read.  A failed read must never block a session start, so it logs at DEBUG only.
    """
    if not get_settings().mcp_scope_enabled:
        return None
    try:
        servers = await account_store_io(configured_server_keys, default=None)
        if servers is None:
            return None
        return effective_disabled(
            servers,
            server_defaults=await store.server_defaults(),
            repo_rules=await store.repo_rules(repo) if repo else {},
            session_overrides=await store.session_overrides(session_id) if session_id else {},
        )
    except Exception:
        log.debug("mcp scope: rules unavailable repo=%s session=%s", repo, session_id, exc_info=True)
        return None
