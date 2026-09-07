"""Per-session single-owner lease — "at most one live CLI per session id" (#1379).

A daemon restart used to resume the SAME session on BOTH fleet hosts. Each host
rehydrates from the shared DB, each concludes the row is its own (the stamp flips
during a cut-short drain, or the peer's stamp has left the registry), and each
spawns its own ``claude --resume <conversation-id>`` against the same worktree.
The twins then race on git, force-push each other's branches, arm auto-merge on
each other's PRs, and split the agent's memory across accounts. Nothing in the
product prevented two owners — ownership was a per-process *opinion*
(``SessionOrchestrator._owns_record``), never a fact both hosts could agree on.

This module makes it a fact, by giving every session the same DB-side lease the
fleet already uses for "exactly one primary daemon" (#852). It is deliberately a
thin naming layer over :mod:`server.sessions.primary_lease` rather than a second
lease implementation:

- one row per session in ``daemon_leases``, keyed ``session:<session_id>``;
- the holder is the HOST name, so a systemd restart re-acquires its own leases
  instantly (``acquire`` always lets the same holder back in) while a PEER host is
  refused until the heartbeat genuinely lapses past the TTL;
- ``fence`` increments on every (re-)acquisition and renew/release are conditional
  on it, so a daemon that was fenced out cannot resurrect its claim.

The lease is a **gate on building a driver**, not on reading a session: a daemon
that loses it logs and skips the spawn, leaving the session to its real owner. It
is advisory in exactly one direction — a DB error fails OPEN (see
``server.sessions.orchestrator._claim_session``), because a database blip must not
stop every host from running sessions.

Two things it deliberately does NOT cover, because the holder is a HOST and not a
process — the choice that lets a restart re-take its own sessions instantly:

* **Two drivers in one process** — already prevented by ``_ensure_lock`` and the
  ``_spawning`` / ``_swapping`` trackers (#1040).
* **A CLI with no daemon behind it** — a process that outlived its unit holds no
  lease and answers to nobody. That is :mod:`server.sessions.orphan_cli`'s job, and
  the two together are what "at most one live CLI per session" actually requires.
"""
from __future__ import annotations

from server.sessions import primary_lease
from server.sessions.primary_lease import LeaseState

__all__ = ["LeaseState", "SESSION_LEASE_PREFIX", "lease_name", "session_id_of",
           "current", "acquire", "renew", "release"]

SESSION_LEASE_PREFIX = "session:"


def lease_name(session_id: str) -> str:
    """The ``daemon_leases`` row key for *session_id*."""
    return f"{SESSION_LEASE_PREFIX}{session_id}"


def session_id_of(name: str) -> str | None:
    """Inverse of :func:`lease_name`; None for a non-session lease row."""
    if not name.startswith(SESSION_LEASE_PREFIX):
        return None
    return name[len(SESSION_LEASE_PREFIX):]


async def current(session_id: str) -> LeaseState | None:
    """The lease row for *session_id*, or None if nobody has ever held it."""
    return await primary_lease.current(name=lease_name(session_id))


async def acquire(session_id: str, holder: str, *, ttl_s: float) -> int | None:
    """Claim *session_id* for *holder*. Returns the fence, or None when a DIFFERENT
    host holds it with a heartbeat fresher than *ttl_s* — i.e. a live twin owner."""
    return await primary_lease.acquire(holder, ttl_s=ttl_s, name=lease_name(session_id))


async def renew(session_id: str, holder: str, fence: int) -> bool:
    """Heartbeat the lease. False means it was fenced away — stop driving the session."""
    return await primary_lease.renew(holder, fence, name=lease_name(session_id))


async def release(session_id: str, holder: str, fence: int) -> None:
    """Hand the lease back (no-op once fenced) so a planned handover skips the TTL."""
    await primary_lease.release(holder, fence, name=lease_name(session_id))
