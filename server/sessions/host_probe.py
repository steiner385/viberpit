"""Probe each enabled host's /info and record health + capacity in the registry.

Failure is recorded explicitly (healthy=False, last_error set) rather than left
stale. Ranking reads these columns, so a stale "healthy" is a dispatch into a
dead host — the registry must say "down", not keep saying what was true an hour
ago.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Awaitable, Callable

from sqlalchemy import select as sa_select, update as sa_update

from server.db import db_session
from server.db.models import Host
from server.deploy.state import DeployState, read_deploy_state
from server.sessions.version_skew import IsAncestor, detect_skew, edge_is_ancestor

log = logging.getLogger(__name__)

Fetch = Callable[[str], Awaitable[dict]]

__all__ = ["probe_host", "probe_all", "probe_loop", "default_fetch"]


async def default_fetch(endpoint: str) -> dict:
    """GET /info from a daemon endpoint, honouring mTLS for https endpoints."""
    from server.ipc.client import DaemonClient
    from server.ipc.paths import parse_endpoint

    client = DaemonClient(parse_endpoint(endpoint))
    try:
        return await client.info()
    finally:
        aclose = getattr(client, "aclose", None)
        if aclose is not None:
            await aclose()


async def probe_host(name: str, endpoint: str, *, fetch: Fetch,
                     state: DeployState | None = None,
                     is_ancestor: IsAncestor = edge_is_ancestor) -> dict:
    """Probe one host and write the outcome. Never raises.

    ``state`` is the deploy_state row read ONCE per tick by probe_all; None disables the
    version-skew gate (no deploy recorded yet, or a caller that wants raw health only).
    """
    from server.ipc.protocol import PROTOCOL_VERSION
    values: dict = {"last_probe_at": datetime.utcnow()}
    try:
        info = await fetch(endpoint)
        daemon_proto = info.get("protocol")
        metrics = dict(sha=info.get("sha"), checkout_sha=info.get("checkoutSha"),
                       protocol=daemon_proto, load_avg=info.get("loadAvg"),
                       mem_available_mb=info.get("memAvailableMb"),
                       active_sessions=info.get("activeSessions"))
        # Gate 1 — protocol: a daemon speaking another RPC protocol is never dispatchable.
        if daemon_proto is not None and daemon_proto != PROTOCOL_VERSION:
            values.update(healthy=False, last_error=(
                f"protocol mismatch: daemon={daemon_proto} edge={PROTOCOL_VERSION}"), **metrics)
        else:
            # Gate 2 — version skew (spec §4): not on the deployed commit ⇒ fenced from
            # dispatch until a deploy fan-out, runner_sync, or an operator fixes it.
            skew = detect_skew(checkout_sha=info.get("checkoutSha"), process_sha=info.get("sha"),
                               state=state, is_ancestor=is_ancestor)
            values.update(healthy=skew is None, last_error=skew, **metrics)
    except Exception as exc:
        # Record the failure; do NOT leave a previous healthy=True in place.
        values.update(healthy=False, last_error=f"{type(exc).__name__}: {exc}"[:500])
        log.warning("fleet.probe_failed host=%s endpoint=%s: %s", name, endpoint, exc)

    async with db_session() as db:
        await db.execute(sa_update(Host).where(Host.name == name).values(**values))
    return values


async def probe_all(*, fetch: Fetch | None = None,
                    is_ancestor: IsAncestor = edge_is_ancestor) -> int:
    """Probe every enabled host. Returns how many were probed.

    Each host is probed independently: one dead host must not hide a live one's
    metrics, so probe_host swallows per-host failures rather than aborting. The
    deploy_state row is read once per tick and passed to every probe_host call so
    all hosts are judged against the same "deployed" snapshot.
    """
    use = fetch or default_fetch
    try:
        state = await read_deploy_state()
    except Exception:
        log.warning("fleet.probe: deploy_state unreadable — skew gate off this tick", exc_info=True)
        state = None
    async with db_session() as db:
        rows = (await db.execute(
            sa_select(Host.name, Host.endpoint).where(Host.enabled.is_(True)))).all()
    for name, endpoint in rows:
        await probe_host(name, endpoint, fetch=use, state=state, is_ancestor=is_ancestor)
    return len(rows)


async def probe_loop(interval_s: float = 30.0, *, fetch: Fetch | None = None,
                     stop: asyncio.Event | None = None) -> None:
    """Probe every enabled host forever, every *interval_s* seconds.

    A raising tick is logged and swallowed: one bad probe must never freeze the
    registry, because stale health is exactly what makes the ranker dispatch into
    a host that is already down.
    """
    while not (stop and stop.is_set()):
        try:
            await probe_all(fetch=fetch)
        except Exception:
            log.exception("fleet.probe_tick_failed")
        try:
            await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            return
