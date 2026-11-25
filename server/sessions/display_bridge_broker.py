"""Edge-side display-bridge broker (spec 2026-11-25-per-host-display-target §2).

The edge, not a daemon, decides which bridges should exist: daemons have no
daemon→daemon RPC and every ``DaemonClient`` lives here. The rule is desired
state, never refcounts — each target daemon is told the FULL set of source hosts
whose sessions draw on its desktop (``display_bridge_ensure``), on spawn, on
archive and on every fleet probe tick. A target that hears nothing for three
probe intervals treats its set as empty (fail closed), so the tick asserts even
an empty set; an edge restart loses nothing because the set lives in the DB.

The desired set is live ``display_mode == "remote"`` sessions grouped by
``display_target_host`` → their source ``host``, plus spawns waiting on
:meth:`DisplayBridgeBroker.prepare`. Reaching a target is an expected degrade,
logged at INFO (WARNING auto-files issues).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from typing import Awaitable, Callable, Iterable, Protocol

from sqlalchemy import select as sa_select

from server.db import db_session
from server.db.models import INACTIVE_SESSION_STATUSES, Host, Session as DbSession
from server.obs.logdedupe import LogThrottle, log_event_throttled
from server.sessions.display_policy import DISPLAY_REMOTE
from server.sessions.display_target import parse_display_target

log = logging.getLogger(__name__)

# The RPC deadline for one ``display_bridge_ensure``. A target tearing a bridge down
# answers only once it is gone (several seconds), so this is deliberately longer than
# a plain read; the spawn wait (``timeout_s``) bounds prepare() separately.
ENSURE_RPC_TIMEOUT_S = 12.0
# How often prepare() re-asks a target whose bridge for the source is still starting.
PREPARE_POLL_S = 0.5
# Target answers that will not turn "up" by waiting: stop the spawn wait at once.
_TERMINAL_STATES = frozenset({"not_accepted", "desktop_unavailable"})


class BridgeClient(Protocol):
    """What the broker needs of a target daemon (``RemoteOrchestrator`` provides it)."""

    async def display_bridge_ensure(self, sources: list[str], ssh_dests: dict[str, str],
                                    *, timeout: float | None = None) -> list[dict]: ...


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
        # target -> source -> number of prepare()s waiting on that bridge right now.
        self._inflight: dict[str, Counter[str]] = {}
        # Sources already reported as lacking an ssh destination (log once each).
        self._no_dest_logged: set[str] = set()
        # Targets this process has asserted a non-empty set to: keep asserting (if only
        # []) so their bridges come down even if they stop advertising a bridge.
        self._asserted: set[str] = set()
        self._unreachable_log = LogThrottle()
        # Serializes reasserts, so an older full set can never land after a newer one.
        self._reassert_lock = asyncio.Lock()

    # ── desired state ──────────────────────────────────────────────────────────────
    async def desired(self) -> dict[str, list[str]]:
        """target → sorted sources: live remote sessions in the DB plus in-flight spawns."""
        async with db_session() as db:
            rows = (await db.execute(
                sa_select(DbSession.display_target_host, DbSession.host).where(
                    DbSession.display_mode == DISPLAY_REMOTE,
                    DbSession.display_target_host.is_not(None),
                    DbSession.host.is_not(None),
                    DbSession.status.not_in(INACTIVE_SESSION_STATUSES)))).all()
        out: dict[str, set[str]] = {}
        for target, source in rows:
            out.setdefault(target, set()).add(source)
        for target, sources in self._inflight.items():
            live = {s for s, n in sources.items() if n > 0}
            if live:
                out.setdefault(target, set()).update(live)
        return {t: sorted(s) for t, s in sorted(out.items())}

    async def target_for(self, source: str) -> str | None:
        """The bare target host *source*'s sessions should draw on, or None (local, the
        kill switch is off, or the registry is unreadable)."""
        if not _enabled():
            return None
        try:
            hosts = await self._hosts()
        except Exception as exc:  # noqa: BLE001 — an unreadable registry reads as local
            log.info("display bridge: host registry unreadable (%s)", type(exc).__name__)
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

    async def _ensure(self, target: str, client: BridgeClient, sources: list[str],
                      dests: dict[str, str]) -> list[dict] | None:
        """One ``display_bridge_ensure``; None (logged at INFO) when the target fails."""
        try:
            bridges = await client.display_bridge_ensure(
                sources, dests, timeout=ENSURE_RPC_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 — an unreachable target is a degrade
            log_event_throttled(
                log, self._unreachable_log, target, logging.INFO,
                "display_bridge.ensure_failed", target=target,
                cause=f"{type(exc).__name__}: {exc}"[:300])
            return None
        self._unreachable_log.forget(target)
        if sources:
            self._asserted.add(target)
        return bridges if isinstance(bridges, list) else []

    # ── spawn ──────────────────────────────────────────────────────────────────────
    async def prepare(self, source: str) -> str | None:
        """Ask *source*'s target to bridge it and wait up to ``timeout_s`` for ``up``.

        Returns the target host name once the target reports the bridge up, else None
        (no target, kill switch off, target disabled/unknown, no ssh destination, the
        target refused, or the wait ran out). The caller passes the target to the source
        daemon either way; its own socket check records the precise fallback reason.
        """
        target = await self.target_for(source)
        if target is None:
            return None
        try:
            hosts = {h.name: h for h in await self._hosts()}
        except Exception as exc:  # noqa: BLE001 — target_for just read it; a flake here
            log.info("display bridge: host registry unreadable (%s)", type(exc).__name__)
            return None
        row = hosts.get(target)
        client = self._clients(target) if row is not None and row.enabled else None
        if client is None:
            log.info("display bridge: target host=%s for source=%s is disabled or not in "
                     "the fleet; not waiting", target, source)
            return None
        if _ssh_dest(hosts.get(source)) is None:
            self._dests_for([source], hosts)
            return None
        counter = self._inflight.setdefault(target, Counter())
        counter[source] += 1
        try:
            return await self._wait_up(target, client, source, hosts)
        finally:
            counter[source] -= 1
            if counter[source] <= 0:
                del counter[source]
            if not counter:
                self._inflight.pop(target, None)

    async def _wait_up(self, target: str, client: BridgeClient, source: str,
                       hosts: dict[str, Host]) -> str | None:
        deadline = time.monotonic() + self._timeout_s
        state = None
        try:
            async with asyncio.timeout(self._timeout_s):
                while True:
                    sources, dests = self._dests_for(
                        (await self.desired()).get(target, []), hosts)
                    bridges = await self._ensure(target, client, sources, dests)
                    state = next((b.get("state") for b in bridges or []
                                  if isinstance(b, dict) and b.get("source") == source),
                                 None)
                    if state == "up":
                        return target
                    if state in _TERMINAL_STATES:
                        break
                    await asyncio.sleep(min(PREPARE_POLL_S,
                                            max(0.0, deadline - time.monotonic())))
        except TimeoutError:
            pass
        log.info("display bridge: source=%s -> target=%s not up (state=%s); the session "
                 "decides virtual or remote from its own sockets", source, target, state)
        return None

    # ── reconcile ──────────────────────────────────────────────────────────────────
    async def reassert(self) -> None:
        """Assert every target's full desired set (``[]`` included). Never raises."""
        if not _enabled():
            return
        try:
            async with self._reassert_lock:
                await self._reassert()
        except Exception:  # noqa: BLE001 — a bad tick must not stop the next one
            log.info("display bridge: reassert failed", exc_info=True)

    async def _reassert(self) -> None:
        hosts = await self._hosts()
        by_name = {h.name: h for h in hosts}
        desired = await self.desired()
        configured = {t for h in hosts if (t := _target_of(h)) is not None}
        targets = sorted(
            h.name for h in hosts
            if h.enabled and (h.display_bridge is not None or h.name in desired
                              or h.name in configured or h.name in self._asserted))

        async def _one(target: str) -> None:
            client = self._clients(target)
            if client is None:
                return
            sources, dests = self._dests_for(desired.get(target, []), by_name)
            if await self._ensure(target, client, sources, dests) is not None and not sources:
                self._asserted.discard(target)

        await asyncio.gather(*(_one(t) for t in targets))
