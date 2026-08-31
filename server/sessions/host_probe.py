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


async def probe_host(name: str, endpoint: str, *, fetch: Fetch) -> dict:
    """Probe one host and write the outcome. Never raises."""
    from server.ipc.protocol import PROTOCOL_VERSION
    values: dict = {"last_probe_at": datetime.utcnow()}
    try:
        info = await fetch(endpoint)
        daemon_proto = info.get("protocol")
        # Gate on protocol version: if the daemon reports a version and it differs
        # from ours, mark unhealthy so the ranker never dispatches to it. A missing
        # protocol field (older daemons) is treated as unknown, not mismatched.
        if daemon_proto is not None and daemon_proto != PROTOCOL_VERSION:
            values.update(
                healthy=False,
                last_error=(
                    f"protocol mismatch: daemon={daemon_proto} edge={PROTOCOL_VERSION}"
                ),
                sha=info.get("sha"),
                protocol=daemon_proto,
                load_avg=info.get("loadAvg"),
                mem_available_mb=info.get("memAvailableMb"),
                active_sessions=info.get("activeSessions"),
            )
        else:
            values.update(
                healthy=True,
                last_error=None,
                sha=info.get("sha"),
                protocol=daemon_proto,
                load_avg=info.get("loadAvg"),
                mem_available_mb=info.get("memAvailableMb"),
                active_sessions=info.get("activeSessions"),
            )
    except Exception as exc:
        # Record the failure; do NOT leave a previous healthy=True in place.
        values.update(healthy=False, last_error=f"{type(exc).__name__}: {exc}"[:500])
        log.warning("fleet.probe_failed host=%s endpoint=%s: %s", name, endpoint, exc)

    async with db_session() as db:
        await db.execute(sa_update(Host).where(Host.name == name).values(**values))
    return values


async def probe_all(*, fetch: Fetch | None = None) -> int:
    """Probe every enabled host. Returns how many were probed.

    Each host is probed independently: one dead host must not hide a live one's
    metrics, so probe_host swallows per-host failures rather than aborting.
    """
    use = fetch or default_fetch
    async with db_session() as db:
        rows = (await db.execute(
            sa_select(Host.name, Host.endpoint).where(Host.enabled.is_(True)))).all()
    for name, endpoint in rows:
        await probe_host(name, endpoint, fetch=use)
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
