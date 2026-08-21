"""Route a session to a host's orchestrator.

Holds a map of host name -> RemoteOrchestrator. Every entry is a REMOTE
orchestrator talking to a daemon; this module must never construct an in-process
SessionOrchestrator. When no host is reachable the correct outcome is
NoHostAvailable, not a local fallback that would run a second orchestrator over
the shared database (the 2026-06-23 "Ready while working" incident, and N hosts
multiply the opportunities for it).
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select as sa_select

from server.db import db_session
from server.db.models import Host
from server.sessions.fleet import HostCandidate, rank_hosts

log = logging.getLogger(__name__)

# Name given to the host seeded from a pre-fleet single-endpoint config.
DEFAULT_HOST_NAME = "local"

__all__ = ["FleetRouter", "UnknownHost", "NoHostAvailable",
           "ensure_default_host", "DEFAULT_HOST_NAME"]


class UnknownHost(Exception):
    """Raised when a caller names a host the router does not hold."""


class NoHostAvailable(Exception):
    """Raised when no host is enabled, healthy and under capacity."""


class FleetRouter:
    """Owns one remote orchestrator per fleet host."""

    def __init__(self) -> None:
        self._orchs: dict[str, object] = {}

    def add(self, name: str, orch: object) -> None:
        self._orchs[name] = orch

    def names(self) -> list[str]:
        return sorted(self._orchs)

    def get(self, name: str):
        try:
            return self._orchs[name]
        except KeyError:
            raise UnknownHost(
                f"no orchestrator for host {name!r}; known hosts: {self.names()}"
            ) from None

    async def candidates(self) -> list[HostCandidate]:
        """Build ranking inputs from the registry, limited to hosts we can reach.

        A registry row we hold no client for is not dispatchable — it would rank
        fine and then fail at call time.
        """
        async with db_session() as db:
            rows = (await db.execute(sa_select(Host))).scalars().all()
        return [
            HostCandidate(
                name=r.name,
                enabled=r.enabled,
                healthy=r.healthy,
                weight=r.weight,
                active_sessions=r.active_sessions or 0,
                max_sessions=r.max_sessions,
                load_avg=r.load_avg,
                mem_available_mb=r.mem_available_mb,
            )
            for r in rows
            if r.name in self._orchs
        ]

    async def pick(self, *, prefer: str | None = None) -> list[str]:
        """Return hosts to try, best first.

        *prefer* moves an explicitly chosen host to the front but does not drop
        the others: a manual choice should still fail over rather than fail.
        """
        if prefer and prefer not in self._orchs:
            raise UnknownHost(
                f"no orchestrator for host {prefer!r}; known hosts: {self.names()}")
        order = rank_hosts(await self.candidates())
        if prefer:
            order = [prefer] + [h for h in order if h != prefer]
        if not order:
            raise NoHostAvailable(
                "no enabled, healthy host with capacity; refusing to run in-process "
                "to avoid a split-brain over the shared database")
        return order


async def ensure_default_host(endpoint: str) -> str:
    """Seed a one-row fleet from a legacy single-endpoint config.

    No-ops when ANY host already exists: once an operator manages the registry,
    startup must not inject rows behind their back.
    """
    async with db_session() as db:
        existing = (await db.execute(
            sa_select(func.count()).select_from(Host))).scalar()
        if existing:
            return DEFAULT_HOST_NAME
        db.add(Host(name=DEFAULT_HOST_NAME, endpoint=endpoint, enabled=True))
    return DEFAULT_HOST_NAME
