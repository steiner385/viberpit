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
from server.obs.logdedupe import LogThrottle, log_event_throttled, log_throttled
from server.sessions.fleet_capacity_alarm import CapacityAlarm
from server.sessions.version_skew import IsAncestor, detect_skew, edge_is_ancestor

log = logging.getLogger(__name__)

# Both loops below re-enter their failure path on EVERY tick of probe_loop (30s), so a
# condition that lasts a day writes ~2880 identical WARNING records. `server.obs.errsink`
# captures WARNING+ and files one auto-error issue per message fingerprint, which is how
# a single unreachable rocky and a single unreachable dobby became four issues with no
# information past their first record: #1507/#1669 (rocky) and #1724/#1676 (dobby).
#
# One throttle per record family — sharing one would let the per-host flood evict the
# deploy-state key. Window, not latch: "is rocky STILL down?" is the first question an
# operator asks, and it has to stay answerable from the journal (see logdedupe's header).
_PROBE_FAILURE_LOG = LogThrottle()
_DEPLOY_STATE_LOG = LogThrottle()

# A lone ``DaemonTimeout`` says the daemon is UP and its event loop was too starved to
# answer /info inside the deadline — on a loaded host that is one stalled tick, and it
# clears by the next probe. Filing an auto-error for it reports the host's load, not a
# fault anyone can fix (#2701: six issues-worth of records from dobby at load 28). Only a
# RUN of them is a condition: this many consecutive unreachable probes for one host (~90s
# at the 30s probe interval) before the record is a WARNING.
#
# ``DaemonUnavailable`` (connection refused) counts in the same run (#2749): the health
# watchdog auto-restarts a wedged daemon, and for its ~10-20s restart window nothing is
# listening. That is neither a deploy restart (``restart_expected``) nor an outage, and the
# wedge-then-restart sequence is one unreachable spell, so a timeout followed by a refusal
# keeps counting. Anything else — a success, a fence, a non-daemon error — ends the run.
UNREACHABLE_ESCALATE_AFTER = 3
# A ``DaemonTimeout`` is stronger evidence of load than of an outage (the daemon answered
# the TCP/TLS handshake), and a runner host's test-suite burst outlasts 90s (#2899), so it
# earns a longer run (~5 min) before escalating. The threshold is that of the CURRENT
# exception's class, so a timeout run that ends in refusals escalates at 3 total.
UNREACHABLE_TIMEOUT_ESCALATE_AFTER = 10
_CONSECUTIVE_UNREACHABLE: dict[str, int] = {}
# Edge-triggered over-cap / no-dispatchable-host alarms, fed once per probe_all tick.
CAPACITY_ALARM = CapacityAlarm()

Fetch = Callable[[str], Awaitable[dict]]

__all__ = ["probe_host", "probe_all", "probe_loop", "default_fetch",
           "FENCED_REACHABLE_PREFIXES"]

# A host fenced by one of the two GATES below is unhealthy for DISPATCH but was reached
# perfectly well over the network — so it is still a deploy TARGET (a deploy is exactly
# what un-fences it). ONE constant, used both when composing the `last_error` text and by
# ``server.deploy.runners.is_deployable`` when reading it back, so the two cannot drift:
# a prefix that stops matching would silently turn "fenced" into "skipped by every deploy".
_SKEW_PREFIX = "version skew"          # composed in server/sessions/version_skew.py
_PROTOCOL_PREFIX = "protocol mismatch"
FENCED_REACHABLE_PREFIXES: tuple[str, ...] = (_SKEW_PREFIX, _PROTOCOL_PREFIX)


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
        _CONSECUTIVE_UNREACHABLE.pop(name, None)
        daemon_proto = info.get("protocol")
        # The engine advertisement is recorded on EVERY outcome below, including a
        # fenced one: it describes what the runner can run, not whether the edge may
        # dispatch to it today, and a stale capability outlives a skew fence. It is
        # deliberately NOT an input to `healthy` — a runner with no Codex binary is a
        # perfectly good Claude runner, and letting Codex touch host health would
        # take Claude dispatch and deploy targeting down with it.
        engines = _engines_snapshot(info)
        metrics = dict(sha=info.get("sha"), checkout_sha=info.get("checkoutSha"),
                       protocol=daemon_proto, load_avg=info.get("loadAvg"),
                       mem_available_mb=info.get("memAvailableMb"),
                       active_sessions=info.get("activeSessions"),
                       engines=engines)
        # Gate 1 — protocol: a daemon speaking another RPC protocol is never dispatchable.
        if daemon_proto is not None and daemon_proto != PROTOCOL_VERSION:
            values.update(healthy=False, last_error=(
                f"{_PROTOCOL_PREFIX}: daemon={daemon_proto} edge={PROTOCOL_VERSION}"), **metrics)
        else:
            # Gate 2 — version skew (spec §4): not on the deployed commit ⇒ fenced from
            # dispatch until a deploy fan-out, runner_sync, or an operator fixes it.
            skew = detect_skew(checkout_sha=info.get("checkoutSha"), process_sha=info.get("sha"),
                               state=state, is_ancestor=is_ancestor)
            values.update(healthy=skew is None, last_error=skew, **metrics)
    except Exception as exc:
        # Record the failure; do NOT leave a previous healthy=True in place.
        values.update(healthy=False, last_error=f"{type(exc).__name__}: {exc}"[:500])
        # healthy=False regardless — dispatch must avoid the host either way. Only the
        # log LEVEL depends on whether the outage is a deploy doing its job (#1614),
        # and only the throttle KEY decides which repeats are news.
        #
        # Keyed host + exception class, which is the granularity the error sink already
        # reads this record at: DaemonTimeout ("/info timed out" — the daemon is up but
        # not answering) and DaemonUnavailable ("All connection attempts failed" —
        # nothing is listening) filed SEPARATE issues per host because they are separate
        # conditions with separate fixes, so neither may sit inside the other's window.
        # Cardinality is hosts x daemon exception classes — bounded by construction.
        #
        # The restart-expected branch carries its own key prefix rather than sharing the
        # host's: an outage that outlives the deploy is the one record worth escalating,
        # and a shared key would let the INFO demotion gag that WARNING for a window.
        #
        # `log_event_throttled`, not `log_throttled`: the host name used to ride the
        # message, so `server.obs.error_signature.signature_of` — which fingerprints on
        # the normalized message, not the throttle key — minted one auto-error issue per
        # host (#2126). ``host`` now rides ``fields`` and the fingerprint is the dotted
        # event name.
        cause = type(exc).__name__
        unreachable = _count_unreachable(name, exc)
        from server.sessions import host_upgrade  # deferred: runners imports this module
        if await host_upgrade.restart_expected(name):
            log_event_throttled(
                log, _PROBE_FAILURE_LOG, f"{name}:restart:{cause}", logging.INFO,
                "fleet.probe_failed", host=name, endpoint=endpoint, error=str(exc),
                restart="in_progress")
        elif 0 < unreachable < escalate_after(exc):
            # Its own key, for the same reason the restart branch has one: sharing the
            # host's would let this INFO gag the WARNING the run is about to earn.
            log_event_throttled(
                log, _PROBE_FAILURE_LOG, f"{name}:transient:{cause}", logging.INFO,
                "fleet.probe_failed", host=name, endpoint=endpoint, error=str(exc),
                consecutive=unreachable)
        else:
            log_event_throttled(
                log, _PROBE_FAILURE_LOG, f"{name}:{cause}", logging.WARNING,
                "fleet.probe_failed", host=name, endpoint=endpoint, error=str(exc))

    async with db_session() as db:
        await db.execute(sa_update(Host).where(Host.name == name).values(**values))
    return values


def unreachable_run(name: str) -> int:
    """Read-only view of *name*'s current consecutive-unreachable run (0 if none)."""
    return _CONSECUTIVE_UNREACHABLE.get(name, 0)


def escalate_after(exc: BaseException) -> int:
    """Run length at which an unreachable probe for *exc*'s class becomes a WARNING."""
    from server.ipc.client import DaemonTimeout  # deferred, as in default_fetch

    # Builtin TimeoutError is the same condition raised by a local asyncio.timeout (#2846).
    if isinstance(exc, (DaemonTimeout, TimeoutError)):
        return UNREACHABLE_TIMEOUT_ESCALATE_AFTER
    return UNREACHABLE_ESCALATE_AFTER


def _count_unreachable(name: str, exc: Exception) -> int:
    """Length of *name*'s current unreachable run including *exc*; 0 if *exc* is not one.

    ``DaemonTimeout`` subclasses ``DaemonUnavailable``, so one isinstance covers both.
    """
    from server.ipc.client import DaemonUnavailable  # deferred, as in default_fetch

    if not isinstance(exc, DaemonUnavailable):
        _CONSECUTIVE_UNREACHABLE.pop(name, None)
        return 0
    _CONSECUTIVE_UNREACHABLE[name] = _CONSECUTIVE_UNREACHABLE.get(name, 0) + 1
    return _CONSECUTIVE_UNREACHABLE[name]


def _engines_snapshot(info: dict) -> dict | None:
    """The `/info.engines` sub-dict to persist, or None for a legacy daemon.

    None is meaningful and must not be normalized away here: the reader
    (``host_engines.parse_engines_info``) is the one place that turns "not
    advertised" into the Claude-only compatibility default, and persisting that
    default would make an upgraded daemon indistinguishable from an old one.
    """
    from server.sessions.host_engines import ENGINES_INFO_KEY

    raw = info.get(ENGINES_INFO_KEY)
    return raw if isinstance(raw, dict) else None


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
        # Same shape, same tick: a deploy_state that stays unreadable repeats this on
        # every probe_all. One key — the condition is global, not per host.
        log_throttled(log, _DEPLOY_STATE_LOG, "deploy_state", logging.WARNING,
                      "fleet.probe: deploy_state unreadable — skew gate off this tick",
                      exc_info=True)
        state = None
    async with db_session() as db:
        rows = (await db.execute(
            sa_select(Host.name, Host.endpoint).where(Host.enabled.is_(True)))).all()
    for name, endpoint in rows:
        await probe_host(name, endpoint, fetch=use, state=state, is_ancestor=is_ancestor)
    await _observe_capacity()
    return len(rows)


async def _observe_capacity() -> None:
    """Feed this tick's registry to the capacity alarm (#1517). Never raises: an
    alarm read that fails must not cost the tick its probes."""
    from server.sessions.fleet import HostCandidate

    try:
        async with db_session() as db:
            hosts = (await db.execute(sa_select(Host))).scalars().all()
        CAPACITY_ALARM.observe(
            [HostCandidate(name=h.name, enabled=h.enabled, healthy=h.healthy,
                           weight=h.weight, active_sessions=h.active_sessions or 0,
                           max_sessions=h.max_sessions, load_avg=h.load_avg,
                           mem_available_mb=h.mem_available_mb)
             for h in hosts],
            deploy_excused=await _deploy_excused(hosts))
    except Exception:
        log.debug("fleet.capacity: alarm check failed", exc_info=True)


async def _deploy_excused(hosts) -> set[str]:
    """Enabled, unhealthy hosts whose outage a deploy explains (#3173): the host's own
    restart step is running (``restart_expected``, as in ``probe_host``), or it was
    reached but fenced on skew/protocol while a deploy is in flight — mid-rollout a
    runner on the new commit is judged against a ``deploy_state`` that still names the
    old one until the edge restart records it."""
    from server.sessions import host_upgrade  # deferred, as in probe_host

    down = [h for h in hosts if h.enabled and h.healthy is not True]
    if not down:
        return set()
    deploying = await host_upgrade.deploy_running()
    return {h.name for h in down
            if await host_upgrade.restart_expected(h.name)
            or (deploying and (h.last_error or "").startswith(FENCED_REACHABLE_PREFIXES))}


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
