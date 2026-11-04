"""Edge grant reconcile (Phase 2): file every MCP OAuth grant under its server's current CLI key.

Each refresher tick the edge (the refresh authority) builds the wanted set from the
configured servers, applies :func:`viberpit.mcp_rekey.plan_rekey` to the shared store under
its lock, and returns the keys that left the store, so the tick's publish purges them from
every account dir. Keys and counts are logged, never token values.
"""
from __future__ import annotations

import logging

from viberpit import creds, mcp_oauth_refresh
from viberpit.mcp_keys import cli_oauth_key, store_name
from viberpit.mcp_rekey import RekeyPlan, WantedServer, plan_rekey

log = logging.getLogger(__name__)

_OAUTH_TRANSPORTS = ("http", "sse")


def is_oauth_candidate(server: dict) -> bool:
    """Whether a scanned server can hold a CLI OAuth grant at a knowable key: http/sse with an
    explicit ``type`` (the CLI hashes ``type`` into the key, so a guessed one names a key it
    never reads)."""
    config = server.get("config")
    return (server.get("transport") in _OAUTH_TRANSPORTS and isinstance(config, dict)
            and "type" in config)


def wanted_servers() -> list[WantedServer]:
    """Every configured http/sse server whose CLI key is knowable.

    See :func:`is_oauth_candidate` for which configs count."""
    from server.library import scan

    wanted: list[WantedServer] = []
    for s in scan.list_mcp_servers()["servers"]:
        if not is_oauth_candidate(s):
            continue
        config = s["config"]
        url = config.get("url")
        if not isinstance(url, str) or not url:
            continue
        name = store_name(s["name"], s.get("pluginName"))
        wanted.append(WantedServer(key=cli_oauth_key(name, config), store_name=name, url=url))
    return wanted


def reconcile_grants() -> frozenset[str]:
    """Move grants to their servers' current keys and drop dead bundles; return the keys to
    purge from the account dirs (dropped keys plus moved bundles' old keys, so capture never
    offers a dir's leftover copy back into the store). Never raises: WARNING + empty set."""
    try:
        wanted = wanted_servers()
        plan: RekeyPlan | None = None

        def apply(store: dict) -> dict:
            nonlocal plan
            # Planned against the locked fresh copy, so a login or refresh landing since the
            # last read is never rolled back. grant_identity does network IO while the store
            # lock is held; acceptable: this runs in the refresher thread, each fetch has its
            # timeout, PRM identities are cached, and a steady-state tick fetches nothing.
            plan = plan_rekey(store, wanted, # The lambda defers the attribute lookup to call time, so a monkeypatch of
            # mcp_oauth_refresh.grant_identity is seen (a bare reference would bind it now).
            lambda prm: mcp_oauth_refresh.grant_identity(prm))
            return plan.store

        creds.update_shared_mcp(apply)
        if plan is None:  # a worker: the store is not ours to change
            return frozenset()
        purge = plan.dropped | frozenset(plan.moved)
        if purge:
            log.info("mcp grants reconciled moved=%s dropped=%s",
                     sorted(f"{old}->{new}" for old, new in plan.moved.items()), sorted(plan.dropped))
        return purge
    except Exception:
        log.warning("mcp grants reconcile failed", exc_info=True)
        return frozenset()
