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

# Fallback name for hosts seeded from a unix-socket endpoint (they really are local).
# HTTPS-endpoint seeds derive their name from the hostname instead.
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


def _derive_host_name(endpoint_str: str) -> str:
    """Derive a short display name from a daemon endpoint string.

    HTTPS endpoints use the first label of their hostname:
      https://rocky.tail0dd570.ts.net:9443  →  rocky
    Unix sockets stay DEFAULT_HOST_NAME ('local') — they really are local.
    """
    try:
        from server.ipc.paths import parse_endpoint
        ep = parse_endpoint(endpoint_str)
        if ep.is_tcp and ep.host:
            return ep.host.split(".")[0]
    except Exception:
        pass
    return DEFAULT_HOST_NAME


async def ensure_default_host(endpoint: str) -> str | None:
    """Resolve which registry host *endpoint* is, seeding a one-row fleet if empty.

    Returns the host name, or None when the registry is operator-managed and does
    not contain this endpoint — in that case we genuinely do not know which host
    we are talking to, and a wrong host stamp is worse than an absent one.

    Seeding no-ops when ANY host already exists: once an operator manages the
    registry, startup must not inject rows behind their back.
    """
    from server.ipc.paths import parse_endpoint

    async with db_session() as db:
        # Already registered — return its name, renaming 'local' → derived if needed.
        row = (await db.execute(
            sa_select(Host).where(Host.endpoint == endpoint))).scalars().first()
        if row is not None:
            if row.name == DEFAULT_HOST_NAME:
                derived = _derive_host_name(endpoint)
                if derived != DEFAULT_HOST_NAME:
                    log.info("fleet: renaming auto-seeded host %r -> %r",
                             row.name, derived)
                    row.name = derived
                    return derived
            return row.name

        # Repair the AUTO-SEEDED row when its endpoint cannot be parsed. #720
        # seeded it with str(endpoint) before Endpoint had a __str__, storing the
        # dataclass repr, and nothing could fix it afterwards: this function
        # no-ops once any host exists and the API refuses to set endpoint.
        # Scoped deliberately — only our own row, only when unusable, so an
        # operator's deliberate endpoint is never rewritten behind their back.
        # Also renames the row when the endpoint resolves to a non-local host. #743
        seeded = (await db.execute(
            sa_select(Host).where(Host.name == DEFAULT_HOST_NAME))).scalars().first()
        if seeded is not None:
            try:
                parse_endpoint(seeded.endpoint)
            except Exception:
                log.warning("fleet: repairing unparseable auto-seeded endpoint %r -> %r",
                            seeded.endpoint, endpoint)
                seeded.endpoint = endpoint
            derived = _derive_host_name(endpoint)
            if derived != DEFAULT_HOST_NAME:
                log.info("fleet: renaming auto-seeded host %r -> %r",
                         seeded.name, derived)
                seeded.name = derived
            return derived
        existing = (await db.execute(
            sa_select(func.count()).select_from(Host))).scalar()
        if existing:
            return None
        derived = _derive_host_name(endpoint)
        db.add(Host(name=derived, endpoint=endpoint, enabled=True))
    return derived
