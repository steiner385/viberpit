"""Post-OAuth follow-through (spec 2026-10-09 section 4): push the new token into every
account cfg dir, reconnect live sessions that were failing on this server, then re-probe."""
from __future__ import annotations

import asyncio
import logging

from viberpit import creds, paths

log = logging.getLogger(__name__)


def _refresh(account: str) -> bool:
    return creds.refresh_cfg_mcp_oauth(paths.account_cfg_dir(account))


async def after_login(orch, server: str, accounts: list[str]) -> dict:  # noqa: ANN001
    refreshed = 0
    for account in accounts:
        try:
            refreshed += bool(await asyncio.to_thread(_refresh, account))
        except Exception:
            log.debug("mcp after_login refresh failed account=%s", account, exc_info=True)
    reconnected = await orch.reconnect_mcp_everywhere(server, ["needs-auth", "failed"])
    try:
        await orch.probe_mcp(account=None)
    except Exception:
        log.debug("mcp after_login probe failed server=%s", server, exc_info=True)
    return {"refreshed": refreshed, "reconnected": reconnected}
