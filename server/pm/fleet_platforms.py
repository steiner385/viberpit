"""The platforms the fleet can run work on right now, and which are occupied.

The queue core (``server.pm.queue``) stays pure: it takes ``platforms`` and
``busy_platforms`` as arguments. This is the one place that derives them from the
registry (``Host.platforms``, the ``/info`` advertisement) and the sessions table, so
the runner, ``GET /api/pm/queue``, the escalation read model and the approve tap read
one answer.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.db.models import Host, Session
from server.pm.desired_state import ACTIVE_SESSION_STATUSES
from server.pm.queue import FLEET_PLATFORMS
from server.sessions.host_platforms import parse_platforms_info


async def fleet_platform_state(db: AsyncSession) -> tuple[frozenset[str], frozenset[str]]:
    """``(advertised, busy)``. *advertised* is Linux plus every platform an enabled,
    healthy host advertises. A non-Linux platform is *busy* when every such advertiser
    already hosts a session pinned to it whose turn is running or queued
    (``ACTIVE_SESSION_STATUSES``): a handed-back or waiting session does not hold the
    VM, so the next issue may start."""
    hosts = (await db.execute(
        select(Host).where(Host.enabled.is_(True), Host.healthy.is_(True)))).scalars().all()
    by_platform: dict[str, set[str]] = {}
    for h in hosts:
        for p in parse_platforms_info(h.platforms) - FLEET_PLATFORMS:
            by_platform.setdefault(p, set()).add(h.name)
    if not by_platform:
        return FLEET_PLATFORMS, frozenset()
    rows = (await db.execute(
        select(Session.host, Session.platform).where(
            Session.platform.in_(by_platform),
            Session.status.in_(ACTIVE_SESSION_STATUSES)))).all()
    held = {(host, platform) for host, platform in rows}
    busy = frozenset(p for p, names in by_platform.items()
                     if all((n, p) in held for n in names))
    return FLEET_PLATFORMS | frozenset(by_platform), busy
