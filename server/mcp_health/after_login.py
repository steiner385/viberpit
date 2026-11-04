"""Post-OAuth follow-through (spec 2026-10-09 section 4): push the new token into every
account cfg dir, clear the server's needs-auth cache, reconnect live sessions that were
failing on this server, then re-probe.

The refresher's heal (MCP auth overhaul phase 3) runs the same follow-through when a
failing grant refreshes again: :func:`heal_servers` calls :func:`after_login` per healed
server without its probe, then probes once."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

from viberpit import accounts as vp_accounts
from viberpit import creds, mcp_oauth_refresh, paths

from server.mcp_auth import clear_needs_auth_cache
from server.mcp_health import grant_health, grants

log = logging.getLogger(__name__)


def _refresh(account: str, server: str | None = None) -> bool:
    return creds.sync_cfg_mcp_oauth(paths.account_cfg_dir(account), server=server)


def _signed_in_keys(server: str) -> list[str]:
    """*server*'s store keys whose current bundle holds a token. A same-short-name sibling
    the sign-in did not write is a tokenless stub; stamping it would later promote it dead."""
    keys = grants.keys_for_server(server)
    store = creds.read_shared_mcp()
    dead = mcp_oauth_refresh.cleared_keys(store)
    return [k for k in keys if isinstance(store.get(k), dict) and k not in dead]


async def _mark_signed_in(server: str) -> None:
    try:
        keys = await asyncio.to_thread(_signed_in_keys, server)
        await grant_health.mark_signed_in(keys, server=server)
    except Exception:
        # A missed mark only delays the new alert generation to the grant's first refresh.
        log.warning("mcp after_login: recording the sign-in failed server=%s", server, exc_info=True)


async def after_login(orch, server: str, accounts: list[str], *,  # noqa: ANN001
                      probe: bool = True, signed_in: bool = False) -> dict:
    """*signed_in* is True only on the interactive sign-in path: it starts a new alert
    generation for the server's grants. *probe* False lets a caller healing several
    servers probe once for all of them."""
    if signed_in:
        await _mark_signed_in(server)
    refreshed = 0
    for account in accounts:
        try:
            refreshed += bool(await asyncio.to_thread(_refresh, account, server))
        except Exception:
            log.debug("mcp after_login refresh failed account=%s", account, exc_info=True)
    try:
        await asyncio.to_thread(clear_needs_auth_cache, server)
    except Exception:
        log.debug("mcp after_login needs-auth cache clear failed server=%s", server, exc_info=True)
    reconnected = 0
    try:
        reconnected = await orch.reconnect_mcp_everywhere(server, ["needs-auth", "failed"])
    except Exception:
        log.debug("mcp after_login reconnect failed server=%s", server, exc_info=True)
    if probe:
        try:
            await orch.probe_mcp(account=None)
        except Exception:
            log.debug("mcp after_login probe failed server=%s", server, exc_info=True)
    return {"refreshed": refreshed, "reconnected": reconnected}


async def heal_servers(orch, servers: Iterable[str]) -> None:  # noqa: ANN001
    """Heal sessions stuck on each of *servers* after its grant refreshed again: the
    after_login follow-through per server, then one probe. Never raises."""
    wanted = sorted(set(servers))
    if not wanted:
        return
    try:
        names = await asyncio.to_thread(vp_accounts._anthropic_account_names)
    except Exception:
        log.warning("mcp heal: listing accounts failed", exc_info=True)
        names = []
    for server in wanted:
        try:
            await after_login(orch, server, names, probe=False)
        except Exception:
            log.warning("mcp heal failed server=%s", server, exc_info=True)
    try:
        await orch.probe_mcp(account=None)
    except Exception:
        log.warning("mcp heal: probe failed", exc_info=True)
