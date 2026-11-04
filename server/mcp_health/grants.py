"""Edge grant reconcile (Phase 2): file every MCP OAuth grant under its server's current CLI key.

Each refresher tick the edge (the refresh authority) builds the wanted set from the
configured servers, applies :func:`viberpit.mcp_rekey.plan_rekey` to the shared store under
its lock, and returns the keys to purge from every account dir in the tick's publish. Grant
identities are fetched before the lock is taken, never under it. Keys and counts are logged,
never token values.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable

from viberpit import creds, mcp_oauth_refresh, paths
from viberpit.mcp_keys import cli_oauth_key, store_name
from viberpit.mcp_rekey import RekeyPlan, WantedServer, plan_rekey

log = logging.getLogger(__name__)

_OAUTH_TRANSPORTS = ("http", "sse")
# A purged key stays in the returned drop set this long, so an account dir that was busy
# (its publish skipped by the stat guard) or offline for a tick still gets it purged.
TOMBSTONE_TTL_S = 30 * 60.0
_tombstones: dict[str, float] = {}


def is_oauth_candidate(server: dict) -> bool:
    """Whether a scanned server can hold a CLI OAuth grant at a knowable key: http/sse with an
    explicit ``type`` (the CLI hashes ``type`` into the key, so a guessed one names a key it
    never reads)."""
    config = server.get("config")
    return (server.get("transport") in _OAUTH_TRANSPORTS and isinstance(config, dict)
            and "type" in config)


def _has_placeholder(config: dict) -> bool:
    """Whether the URL or a header value holds an unexpanded ``${…}``: the CLI hashes the
    expanded value, so a key computed from the raw config would be wrong."""
    headers = config.get("headers")
    values = [config.get("url"), *(headers.values() if isinstance(headers, dict) else ())]
    return any(isinstance(v, str) and "${" in v for v in values)


def key_for(server: dict) -> str | None:
    """The CLI's OAuth key for a scanned server, or None when it has none we can know: not an
    OAuth candidate (:func:`is_oauth_candidate`), no URL, or a ``${…}`` in the URL or a
    header value."""
    if not is_oauth_candidate(server):
        return None
    config = server["config"]
    url = config.get("url")
    if not isinstance(url, str) or not url or _has_placeholder(config):
        return None
    return cli_oauth_key(store_name(server["name"], server.get("pluginName")), config)


def wanted_servers() -> list[WantedServer]:
    """Every configured server with a knowable CLI key (:func:`key_for`)."""
    from server.library import scan

    wanted: list[WantedServer] = []
    for s in scan.list_mcp_servers()["servers"]:
        key = key_for(s)
        if key is not None:
            wanted.append(WantedServer(key=key, store_name=store_name(s["name"], s.get("pluginName")),
                                       url=s["config"]["url"]))
    return wanted


def _user_config_unreadable() -> bool:
    """True when ``claude.shared.json`` exists but is not a readable JSON object. Absent is
    fine (no user servers); unreadable is not: every user server would look unconfigured."""
    path = paths.shared_claude_json_path()
    if not path.exists():
        return False
    try:
        return not isinstance(json.loads(path.read_text()), dict)
    except (OSError, ValueError):
        return True


def _live_tombstones(now: float, *, present: dict) -> frozenset[str]:
    """Unexpired tombstoned keys, minus any key back in the store (*present*)."""
    for key in [k for k, until in _tombstones.items() if until <= now]:
        del _tombstones[key]
    return frozenset(k for k in _tombstones if k not in present)


def reconcile_grants(*, clock: Callable[[], float] = time.monotonic) -> frozenset[str]:
    """Move grants to their servers' current keys and drop dead bundles; return the keys to
    purge from the account dirs: this tick's dropped keys and moved bundles' old keys (so
    capture never offers a dir's leftover copy back), plus every key purged within
    :data:`TOMBSTONE_TTL_S` that is not back in the store. A dir copy holding a refresh token
    is never purged (``creds.publish_mcp``). Never raises: WARNING + empty set."""
    try:
        now = clock()
        snapshot = creds.read_shared_mcp()
        if _user_config_unreadable():
            log.warning("mcp grants reconcile skipped: claude.shared.json is unreadable")
            return _live_tombstones(now, present=snapshot)
        from server.library import scan

        wanted = wanted_servers()
        enabled_plugins = scan.enabled_plugin_names()

        # Dry run on the snapshot, outside the store lock, to fetch every identity the plan
        # needs; the locked run below only looks them up, so no network IO happens under it.
        prefetched: dict[str, tuple[str, str] | None] = {}

        def fetch(prm: str) -> tuple[str, str] | None:
            if prm not in prefetched:
                prefetched[prm] = mcp_oauth_refresh.grant_identity(prm)
            return prefetched[prm]

        plan_rekey(snapshot, wanted, fetch, enabled_plugins)
        plan: RekeyPlan | None = None

        def apply(store: dict) -> dict:
            nonlocal plan
            # Planned against the locked fresh copy, so a login or refresh landing since the
            # snapshot is never rolled back. An identity not prefetched is unknown (None),
            # which never moves or drops anything.
            plan = plan_rekey(store, wanted, prefetched.get, enabled_plugins)
            return plan.store

        creds.update_shared_mcp(apply)
        if plan is None:  # a worker: the store is not ours to change
            return frozenset()
        purge = plan.dropped | frozenset(plan.moved)
        if purge:
            log.info("mcp grants reconciled moved=%s dropped=%s",
                     sorted(f"{old}->{new}" for old, new in plan.moved.items()), sorted(plan.dropped))
        for key in purge:
            _tombstones[key] = now + TOMBSTONE_TTL_S
        return _live_tombstones(now, present=plan.store)  # includes this tick's purge
    except Exception:
        log.warning("mcp grants reconcile failed", exc_info=True)
        return frozenset()
