"""Edge-side display-bridge broker (spec 2026-11-25-per-host-display-target §2).

The edge, not a daemon, decides which bridges should exist: daemons have no
daemon→daemon RPC and every ``DaemonClient`` lives here. The rule is desired
state, never refcounts — each target daemon is told the FULL set of source hosts
whose sessions draw on its desktop (``display_bridge_ensure``), on spawn, on
archive and on every fleet probe tick. A target that hears nothing for three
probe intervals treats its set as empty (fail closed), so the tick asserts even
an empty set; an edge restart loses nothing because the set lives in the DB.

The desired set is
* live ``display_mode == "remote"`` sessions grouped by ``display_target_host`` →
  their source ``host``, kept only while that source host's CURRENT target is still
  that host (case-insensitive) — a re-pointed or moved/adopted row wants no bridge;
* queued / held sessions that may still go remote at actual spawn (no decision yet,
  or virtual for a bridge reason) under their source host's CURRENT target — the
  source daemon re-resolves their remote leg when they start;
* spawns in flight: :meth:`DisplayBridgeBroker.prepare` holds a claim from before
  the first ``ensure`` until the source daemon's ``create_session`` has returned
  (and so written the row that keeps the source desired) — or, when that RPC's
  outcome is unknown, for one more start timeout.

Every ensure for one target — a prepare poll or a reassert — computes the set and
sends it under that target's lock, so an older full set can never land after a
newer one. Reaching a target is an expected degrade, logged at INFO (WARNING
auto-files issues).

Only hosts the edge's ``FleetRouter`` knows (enabled at edge start) have a client:
a host added to the fleet later is never ensured until the edge restarts — the
same limitation as dispatching sessions to it.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable, Iterable, Protocol

from sqlalchemy import or_, select as sa_select

from server.db import db_session
from server.db.models import INACTIVE_SESSION_STATUSES, Host, Session as DbSession
from server.ipc.client import DaemonError, DaemonUnavailable
from server.obs.logdedupe import LogThrottle, log_event_throttled
from server.sessions.display_bridge import STATE_UP, TARGET_STATE_HINTS
from server.sessions.display_bridge_env import BRIDGE_FALLBACK_REASONS
from server.sessions.display_policy import DISPLAY_REMOTE, DISPLAY_VIRTUAL
from server.sessions.display_target import parse_display_target

log = logging.getLogger(__name__)

# The RPC deadline for one ``display_bridge_ensure``. A target tearing a bridge down
# answers only once it is gone (several seconds), so this is deliberately longer than
# a plain read; the spawn wait (``timeout_s``) bounds prepare() separately.
ENSURE_RPC_TIMEOUT_S = 12.0
# How often prepare() re-asks a target whose bridge for the source is still starting.
PREPARE_POLL_S = 0.5
# Sessions not started yet: a queued row and a model-approval hold. Their display
# decision is (re-)resolved at actual spawn, so the bridge must already be wanted.
_AWAITING_SPAWN_STATUSES = frozenset({"queued", "pending_approval"})


class BridgeClient(Protocol):
    """What the broker needs of a target daemon (``RemoteOrchestrator`` provides it)."""

    async def display_bridge_ensure(self, sources: list[str], ssh_dests: dict[str, str],
                                    *, timeout: float | None = None) -> list[dict]: ...


@dataclass(frozen=True)
class PreparedBridge:
    """What a spawn learned: the source's ``target`` (None = local), whether the target
    reported the bridge ``up``, and the fallback ``hint`` the target's state implies
    (``TARGET_STATE_HINTS``) for the source daemon to record if its own check fails."""
    target: str | None
    up: bool = False
    hint: str | None = None


async def _all_hosts() -> list[Host]:
    async with db_session() as db:
        return list((await db.execute(sa_select(Host))).scalars().all())


def _enabled() -> bool:
    from server.config import get_settings
    return bool(getattr(get_settings(), "display_bridge_enabled", True))


def _target_of(host: Host) -> str | None:
    """*host*'s configured display target, or None (local, or an unparseable value —
    writes are validated, so that is a hand-edited row; treat it as local)."""
    try:
        return parse_display_target(host.display_target)
    except ValueError:
        return None


def _ssh_dest(host: Host | None) -> str | None:
    """The login ON the source host the target's bridge ssh's into."""
    if host is None:
        return None
    return (host.display_ssh_host or host.ssh_host or "").strip() or None


class DisplayBridgeBroker:
    def __init__(self, *, clients: Callable[[str], BridgeClient | None],
                 hosts: Callable[[], Awaitable[list[Host]]] | None = None,
                 timeout_s: float) -> None:
        self._clients = clients
        self._hosts = hosts or _all_hosts
        self._timeout_s = timeout_s
        # target -> source -> number of spawns holding a claim on that bridge right now.
        self._inflight: dict[str, Counter[str]] = {}
        # Sources already reported as lacking an ssh destination (log once each).
        self._no_dest_logged: set[str] = set()
        # Targets this process has asserted a non-empty set to: keep asserting (if only
        # []) so their bridges come down even if they stop advertising a bridge.
        self._asserted: set[str] = set()
        self._unreachable_log = LogThrottle()
        # Targets whose daemon predates ``display_bridge_ensure`` (UnknownMethod): a
        # terminal state for prepare (no polling), logged once until it answers again.
        self._outdated: set[str] = set()
        # target -> lock serializing "compute the set, send it" for that target.
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, target: str) -> asyncio.Lock:
        return self._locks.setdefault(target, asyncio.Lock())

    async def hosts(self) -> list[Host]:
        """The host registry, read once per caller pass (spawns pass it back in)."""
        return await self._hosts()

    # ── desired state ──────────────────────────────────────────────────────────────
    async def desired(self, hosts: list[Host] | None = None) -> dict[str, list[str]]:
        """target → sorted sources (see the module docstring for the three inputs)."""
        if hosts is None:
            hosts = await self._hosts()
        current = {h.name: t for h in hosts if (t := _target_of(h)) is not None}
        async with db_session() as db:
            live = (await db.execute(
                sa_select(DbSession.display_target_host, DbSession.host).where(
                    DbSession.display_mode == DISPLAY_REMOTE,
                    DbSession.display_target_host.is_not(None),
                    DbSession.host.is_not(None),
                    DbSession.status.not_in(INACTIVE_SESSION_STATUSES)))).all()
            waiting = (await db.execute(
                sa_select(DbSession.host).where(
                    DbSession.status.in_(_AWAITING_SPAWN_STATUSES),
                    DbSession.host.in_(list(current)),
                    or_(DbSession.display_mode.is_(None),
                        (DbSession.display_mode == DISPLAY_VIRTUAL)
                        & DbSession.display_fallback_reason.in_(
                            sorted(BRIDGE_FALLBACK_REASONS)))))).scalars().all()
        out: dict[str, set[str]] = {}
        for stored, source in live:
            # Only while the source still targets that host: after a target change (or
            # a move/adoption onto a host with another target) the row's bridge is not
            # wanted — two targets must never bridge one source's shared fixed paths.
            target = current.get(source)
            if target is not None and target.lower() == stored.lower():
                out.setdefault(target, set()).add(source)
        for source in waiting:
            if source is not None:          # the query filters NULL hosts out
                out.setdefault(current[source], set()).add(source)
        for target, sources in self._inflight.items():
            claimed = {s for s, n in sources.items() if n > 0}
            if claimed:
                out.setdefault(target, set()).update(claimed)
        return {t: sorted(s) for t, s in sorted(out.items())}

    def target_for(self, source: str, hosts: list[Host]) -> str | None:
        """The bare target host *source*'s sessions should draw on, or None (local, or
        the kill switch is off)."""
        if not _enabled():
            return None
        row = next((h for h in hosts if h.name == source), None)
        target = _target_of(row) if row is not None else None
        if target is None or target.lower() == source.lower():
            return None
        return target

    def _dests_for(self, sources: Iterable[str], by_name: dict[str, Host]) -> tuple[
            list[str], dict[str, str]]:
        """Keep the sources that have an ssh destination; log the rest once each."""
        kept: list[str] = []
        dests: dict[str, str] = {}
        for source in sources:
            dest = _ssh_dest(by_name.get(source))
            if dest is None:
                if source not in self._no_dest_logged:
                    self._no_dest_logged.add(source)
                    log.info("display bridge: source host=%s has no display_ssh_host or "
                             "ssh_host; it cannot be bridged", source)
                continue
            self._no_dest_logged.discard(source)
            kept.append(source)
            dests[source] = dest
        return kept, dests

    async def _assert(self, target: str, client: BridgeClient,
                      hosts: list[Host]) -> list[dict] | None:
        """Compute *target*'s full set and send it, under the target's lock. Returns the
        bridges, or None (logged at INFO) when the target could not be reached."""
        by_name = {h.name: h for h in hosts}
        async with self._lock_for(target):
            sources, dests = self._dests_for(
                (await self.desired(hosts)).get(target, []), by_name)
            try:
                bridges = await client.display_bridge_ensure(
                    sources, dests, timeout=ENSURE_RPC_TIMEOUT_S)
            except DaemonError as exc:
                if exc.err_type != "UnknownMethod":
                    self._log_unreachable(target, exc)
                elif target not in self._outdated:
                    self._outdated.add(target)
                    log.info("display bridge: target host=%s predates display_bridge_ensure; "
                             "upgrade its daemon to bridge onto its desktop", target)
                return None
            except Exception as exc:  # noqa: BLE001 — an unreachable target is a degrade
                self._log_unreachable(target, exc)
                return None
            self._unreachable_log.forget(target)
            self._outdated.discard(target)
            if sources:
                self._asserted.add(target)
            else:
                self._asserted.discard(target)
            return bridges if isinstance(bridges, list) else []

    def _log_unreachable(self, target: str, exc: BaseException) -> None:
        log_event_throttled(
            log, self._unreachable_log, target, logging.INFO,
            "display_bridge.ensure_failed", target=target,
            cause=f"{type(exc).__name__}: {exc}"[:300])

    # ── spawn ──────────────────────────────────────────────────────────────────────
    @asynccontextmanager
    async def prepare(self, source: str, *, hosts: list[Host] | None = None,
                      wait: bool = True) -> AsyncIterator[PreparedBridge]:
        """Claim *source*'s bridge on its target for the body of the ``async with``.

        Wrap the source daemon's ``create_session`` in it: the claim keeps *source* in
        the target's desired set until that RPC has returned (the row it writes keeps it
        there afterwards), so no tick or archive can tear down the bridge a spawn waited
        for. With *wait*, asks the target to bridge *source* and waits up to
        ``timeout_s`` for ``up``. Yields ``PreparedBridge(target=None)`` for a local
        source / kill switch off; ``up=False`` when the target is disabled or not in the
        fleet, the source has no ssh destination, the target refused, or time ran out.
        """
        if hosts is None:
            hosts = await self._hosts()
        target = self.target_for(source, hosts)
        if target is None:
            yield PreparedBridge(None)
            return
        by_name = {h.name: h for h in hosts}
        row = by_name.get(target)
        client = self._clients(target) if row is not None and row.enabled else None
        if client is None:
            log.info("display bridge: target host=%s for source=%s is disabled or not in "
                     "the fleet; not waiting", target, source)
            yield PreparedBridge(target)
            return
        if _ssh_dest(by_name.get(source)) is None:
            self._dests_for([source], by_name)
            yield PreparedBridge(target)
            return
        self._inflight.setdefault(target, Counter())[source] += 1
        outcome_unknown = False
        try:
            result = (await self._wait_up(target, client, source, hosts) if wait
                      else PreparedBridge(target))
            yield result
        except DaemonUnavailable as exc:
            outcome_unknown = exc.outcome_unknown
            raise
        finally:
            if outcome_unknown:
                # The create RPC reached the source daemon and lost its answer: it may
                # still be writing the row that keeps *source* desired. Keep the claim
                # for one start timeout (a fixed grace — simpler than watching for the
                # row), then release; a row written by then is what the tick sees.
                asyncio.get_running_loop().call_later(
                    self._timeout_s, self._release, target, source)
            else:
                self._release(target, source)

    def _release(self, target: str, source: str) -> None:
        """Drop one spawn's claim on (*target*, *source*)."""
        counter = self._inflight.get(target)
        if counter is None:
            return
        counter[source] -= 1
        if counter[source] <= 0:
            del counter[source]
        if not counter:
            self._inflight.pop(target, None)

    async def _wait_up(self, target: str, client: BridgeClient, source: str,
                       hosts: list[Host]) -> PreparedBridge:
        deadline = time.monotonic() + self._timeout_s
        state = None
        try:
            async with asyncio.timeout(self._timeout_s):
                while True:
                    bridges = await self._assert(target, client, hosts)
                    if bridges is None and target in self._outdated:
                        break      # an older daemon: waiting cannot make it bridge
                    # The target lowercases sources (display_bridge.DisplayBridge.ensure).
                    state = next((b.get("state") for b in bridges or []
                                  if isinstance(b, dict)
                                  and str(b.get("source", "")).lower() == source.lower()),
                                 None)
                    if state == STATE_UP:
                        return PreparedBridge(target, up=True)
                    if state in TARGET_STATE_HINTS:
                        break
                    await asyncio.sleep(min(PREPARE_POLL_S,
                                            max(0.0, deadline - time.monotonic())))
        except TimeoutError:
            pass
        log.info("display bridge: source=%s -> target=%s not up (state=%s); the session "
                 "decides virtual or remote from its own sockets", source, target, state)
        return PreparedBridge(target, hint=TARGET_STATE_HINTS.get(state or ""))

    # ── reconcile ──────────────────────────────────────────────────────────────────
    async def reassert(self) -> None:
        """Assert every target's full desired set (``[]`` included). Never raises."""
        if not _enabled():
            return
        try:
            await self._reassert()
        except Exception:  # noqa: BLE001 — a bad tick must not stop the next one
            log.info("display bridge: reassert failed", exc_info=True)

    async def _reassert(self) -> None:
        hosts = await self._hosts()
        desired = await self.desired(hosts)
        configured = {t for h in hosts if (t := _target_of(h)) is not None}
        targets = sorted(
            h.name for h in hosts
            if h.enabled and (h.display_bridge is not None or h.name in desired
                              or h.name in configured or h.name in self._asserted))

        async def _one(target: str) -> None:
            client = self._clients(target)
            if client is not None:
                await self._assert(target, client, hosts)

        await asyncio.gather(*(_one(t) for t in targets))
