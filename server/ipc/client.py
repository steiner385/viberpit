"""Edge-side client for the session daemon over a Unix socket or mTLS TCP."""
import asyncio
import json
import logging
import os
import time
from typing import Any

import httpx
import websockets

from server.ipc import protocol as proto
from server.ipc.paths import Endpoint, parse_endpoint

log = logging.getLogger("server.ipc.client")

# --------------------------------------------------------------------------- #
# HTTP client bounds — ONE definition, applied identically to the unix and TLS
# branches below so the two constructions can never drift.
#
# Before this, the client passed a bare `timeout=30.0`. httpx expands a scalar to
# connect = read = write = POOL = 30s, and with no `limits=` it inherited
# max_connections=100. So a request that could not get a pool slot waited the full
# 30s before failing — which is exactly what every daemon-backed route did while the
# pool held stranded connections: 503 after exactly 30s (issue #734).
# --------------------------------------------------------------------------- #

# max_connections: unchanged from httpx's default. GET /api/sessions still fans out
#   ~3 RPCs per session concurrently, so a smaller ceiling would turn a busy page load
#   into pool queueing and degrade enrichment fields. Pinned explicitly so it is a
#   deliberate number that moves with the fan-out (see #742) rather than a library one.
# max_keepalive_connections: above the 20 default so a fan-out burst reuses its
#   connections on the next poll instead of closing 80% of them every time.
# keepalive_expiry: httpx's 5s default, made explicit. Short on purpose — an idle
#   connection to a daemon that has since restarted must be discarded, not handed out.
_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=32,
                       keepalive_expiry=5.0)

# The RESERVED HEALTH CHANNEL — a pool of exactly one connection, used by `/info` and
# nothing else.
#
# `/info` carries the daemon's process epoch, and a changed epoch is the ONLY signal that
# triggers `_recycle_transport`. While `/info` shared the RPC pool that signal could be
# starved by the very condition it exists to clear: with all `_LIMITS.max_connections`
# slots held by connections the pool cannot evict, `/info` raised `PoolTimeout` after the
# pool leg, `_reconcile_epoch` swallowed it, and the recycle never fired. The WS reader
# calls `_reconcile_epoch` once per reconnect, so a pool that was already saturated when the
# daemon restarted stayed wedged for the life of the edge process — the state both
# production outages reached, each cleared only by `systemctl restart ccswitch.service`.
#
# One extra socket buys a channel the RPC pool can never starve, which makes the wedge
# self-healing. `max_connections=1` is deliberate rather than incidental: `/info` has one
# low-frequency caller on this client (`_reconcile_epoch`, once per WS reconnect) plus the
# daemon-health poller's own separate client, so a second concurrent `/info` may queue —
# and queueing behind another *health probe* is not the failure mode this guards against.
#
# Note this is NOT "recycle on PoolTimeout". That was considered and rejected: a
# legitimate fan-out saturating the pool under a slow daemon would kill ~100 healthy
# in-flight requests. Isolating `/info` gets the signal through without ever making
# pool pressure itself the trigger.
_INFO_LIMITS = httpx.Limits(max_connections=1, max_keepalive_connections=1,
                            keepalive_expiry=5.0)

# Minimum gap between pool warnings within one streak (see _note_pool_timeout).
# 30s is short enough that a wedge is visible within one journal screen and long enough
# that a polling dashboard cannot turn the wedge into the log flood that hides it.
_POOL_WARN_INTERVAL_S = 30.0

# How long the pool may go without handing ANY caller a slot before a pool timeout is
# called exhaustion rather than queueing (see _note_pool_timeout). Under load the pool
# turns over continuously — every completed, timed-out or reset RPC returns its slot — so
# a gap this long against a pool that is still refusing slots is not a busy moment.
_WEDGE_QUIET_S = 30.0

# The pool-acquisition ceiling. It MUST outlast the slowest expected RPC: a caller queued
# behind a full pool is waiting for one in-flight RPC to return its slot, and
# `transcript_events` was measured at 4.3s, so a leg below that sheds load the pool would
# have served. That is not hypothetical — at max_connections=100 and ~63 RPCs per page
# fan-out, two concurrent dashboard loads leave ~26 RPCs queued, and the old 2s leg failed
# every one of them with empty enrichment fields while the daemon was perfectly healthy.
# 6s is 4.3s plus headroom and still 5x cheaper than the read leg, so a caller that really
# cannot get a slot still fails long before the browser gives up.
_POOL_ACQUIRE_S = 6.0

# Bounded but generous enough for a TLS handshake over the relayed tailnet link
# (~200ms round trips today). Named rather than inlined because `_call_timeout` caps
# each caller's connect leg against it, and reading it back off `_TIMEOUT` would both
# duplicate the source of truth and widen the type to `float | None` (httpx stores an
# unset leg as None), which is not what this value is.
_CONNECT_S = 5.0

# read: kept at 30s — some RPCs legitimately take seconds (transcript_events was
#   measured at 4.3s); longer ones (automation_*) pass their own per-call timeout.
# write: matched to read; request bodies are small, but a stalled write must not hang.
_TIMEOUT = httpx.Timeout(connect=_CONNECT_S, read=30.0, write=30.0, pool=_POOL_ACQUIRE_S)


def _call_timeout(timeout: float) -> httpx.Timeout:
    """Expand a per-call deadline into per-leg budgets, bounding the pool leg BOTH ways.

    httpx turns a scalar ``timeout=`` into connect = read = write = **pool**, so a
    long per-call deadline (automation_* passes 90s) would also let a starved caller
    wait 90s for a connection slot — the very shape this module bounds. Hence the
    ``_POOL_ACQUIRE_S`` ceiling.

    The ``min`` is the other half, and it is the one that was wrong: the pool leg must
    also never be SHORTER than the caller's own budget. With a fixed 2s leg, an
    enrichment RPC that had declared a 3s budget was still shed at 2s — the queue was
    abandoned a second before the caller would have given up. Now a caller spends its
    whole budget waiting for a slot if it has to, and never more than that (waiting past
    your own deadline is work nobody is going to read). connect is capped the same way.
    """
    return httpx.Timeout(connect=min(timeout, _CONNECT_S), read=timeout,
                         write=timeout, pool=min(timeout, _POOL_ACQUIRE_S))

# Base subscriber queue size. Scales up automatically with active stream count;
# override with CCSWITCH_IPC_QUEUE_SIZE to tune without code changes.
_BASE_Q_SIZE = int(os.environ.get("CCSWITCH_IPC_QUEUE_SIZE", "1024"))

# Event types that must reach the browser to preserve session UX: a dropped
# permissionRequest leaves the model permanently blocked waiting for a decision
# it will never get; a dropped questionResolved lets the resolved card re-appear
# on every mid-turn reattach. These never go through the droppable eviction path.
_CTRL_EVENT_TYPES: frozenset[str] = frozenset({
    "permissionRequest",
    "permissionResolved",
    "question",
    "questionResolved",
})


def protocol_compatible(daemon_protocol: int) -> bool:
    """True when the daemon's wire protocol matches this build's."""
    from server.ipc import protocol as _proto
    return daemon_protocol == _proto.PROTOCOL_VERSION


class _SeqDict(dict):
    """An event dict that also carries the daemon's per-stream source seq.

    Subclasses ``dict`` so it is content-equal to (and JSON-serializes identically
    to) the plain event — every existing subscriber and equality-based test is
    unaffected — while ws._forward_live can read ``.src_seq`` to stamp the additive
    ``srcSeq`` loss telemetry (audit W2.3). ``None`` when the delivered frame has no
    daemon seq (a GAP sentinel or a locally-injected control event). The seq lives
    on an attribute, never a dict KEY, so it never leaks into the wire payload.
    """
    __slots__ = ("src_seq",)

    def __init__(self, data, src_seq=None):
        super().__init__(data)
        self.src_seq = src_seq


class DaemonUnavailable(Exception):
    """The daemon socket could not be reached."""


class DaemonTimeout(DaemonUnavailable):
    """An RPC exceeded its deadline, enforced natively by httpx.

    A subclass of :class:`DaemonUnavailable` so every existing handler keeps behaving
    exactly as before (a timeout has always been "daemon unreachable" to them). It
    exists so the handful of callers that used to distinguish ``asyncio.TimeoutError``
    (deadline expired — degrade and tell the user to retry) from a transport failure
    (daemon down — give up) can still tell them apart now that the deadline is pushed
    into the RPC instead of raced from outside with ``asyncio.wait_for``.
    """


class DaemonError(Exception):
    """The daemon returned an error envelope for an RPC call."""

    def __init__(self, err_type: str, message: str, data: dict | None = None):
        super().__init__(f"{err_type}: {message}")
        self.err_type = err_type
        self.message = message
        # Structured payload carried by typed daemon exceptions (e.g.
        # AllAccountsBusy.error_data) so a remote caller can reconstruct them.
        self.data = data


def daemon_ws_connect(endpoint: Endpoint, path: str, ssl_ctx=None):
    """Return a websockets connect context manager for a daemon WS path.

    Single source of truth for opening edge→daemon WebSockets (events + vnc).
    `path` is like "/events" or "/vnc?session=abc".
    """
    if endpoint.is_unix:
        return websockets.unix_connect(endpoint.path, f"ws://daemon{path}")
    return websockets.connect(f"wss://{endpoint.host}:{endpoint.port}{path}", ssl=ssl_ctx)


class DaemonClient:
    def __init__(self, endpoint, *, client_tls=None):
        if isinstance(endpoint, str):
            endpoint = parse_endpoint(endpoint if "://" in endpoint else f"unix://{endpoint}")
        self.endpoint = endpoint
        # Both branches build the transport EXPLICITLY, for two reasons. The explicit
        # transport owns the pool, so `limits` belongs on it — AsyncClient(limits=…) is
        # ignored whenever a transport is supplied. And `_recycle_transport` needs a
        # handle on the pool it has to discard; owning the object beats reaching into
        # httpx's private `_transport`/`_mounts`. (Passing a transport also means an
        # ambient HTTPS_PROXY can no longer intercept the daemon link — the daemon is a
        # direct unix/tailnet peer and must never be proxied.)
        if endpoint.is_unix:
            self._ssl = None
            self._transport = httpx.AsyncHTTPTransport(uds=endpoint.path, limits=_LIMITS)
            self._info_transport = httpx.AsyncHTTPTransport(uds=endpoint.path,
                                                           limits=_INFO_LIMITS)
            base_url = "http://daemon"
        else:
            from server.ipc.tls import client_ssl_context, client_tls_files
            self._ssl = client_ssl_context(client_tls or client_tls_files())
            self._transport = httpx.AsyncHTTPTransport(verify=self._ssl, limits=_LIMITS)
            self._info_transport = httpx.AsyncHTTPTransport(verify=self._ssl,
                                                            limits=_INFO_LIMITS)
            base_url = f"https://{endpoint.host}:{endpoint.port}"
        self._http = httpx.AsyncClient(transport=self._transport, base_url=base_url,
                                       timeout=_TIMEOUT)
        # Same bounds, own pool (see _INFO_LIMITS). A separate AsyncClient is required,
        # not just a separate transport: httpx picks the transport per client, and the
        # RPC client must keep _transport as its one and only route so `call` can never
        # borrow the reserved socket.
        self._info_http = httpx.AsyncClient(transport=self._info_transport,
                                            base_url=base_url, timeout=_TIMEOUT)
        # Wedge telemetry. A pool timeout means "every slot is taken and none freed within
        # the pool leg" — a shape a busy fan-out also reaches, which is why the strand
        # verdict needs `_pool_is_exhausted` on top of it (see `_note_pool_timeout`).
        # Counted here so both outages' `ss -tn state close-wait` dump is readable from
        # `pool_stats()` and loud in the journal without shelling in.
        self._pool_timeouts = 0                           # cumulative, process lifetime
        self._consecutive_pool_timeouts = 0               # reset by any RPC that completes
        self._last_pool_warn_at = 0.0                     # monotonic; throttles the warning
        # Monotonic time of the last request that got PAST the pool — answered, timed out
        # on the wire, or reset. All three prove the pool handed out a slot, which is the
        # signal that separates "queued behind a fan-out" from "nothing is ever freed"
        # (see _note_pool_timeout). Seeded at construction so a brand-new client that
        # immediately queues is never mistaken for a wedge.
        self._last_slot_at = time.monotonic()
        self._ws: Any = None
        self._reader_task: asyncio.Task | None = None
        # stream -> set of subscriber queues. A stream may have MORE THAN ONE local
        # subscriber (e.g. the same session open in two tabs/devices, both served by
        # this one shared edge client). Each gets its own queue; _deliver fans out to
        # all of them. Previously this was a single queue per stream that subscribe()
        # overwrote, so a second subscriber silently starved the first — the live
        # transcript went dead until a detach/reattach forced a fresh disk replay.
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        # queue -> events dropped since its last emitted GAP. A full queue drops its
        # oldest event; the drop is otherwise invisible (the daemon watermark already
        # advanced, so a reconnect never re-sends it). _iter drains this into ONE GAP
        # per overflow burst so the consumer resyncs from disk. Keyed by identity;
        # popped when the subscriber is discarded (single loop, no locking).
        self._dropped: dict[asyncio.Queue, int] = {}
        # queue -> list of control events stashed when the queue was full. Control events
        # must never be evicted (see _CTRL_EVENT_TYPES). _iter re-injects them into the
        # main queue as slots free so they are delivered in original arrival order.
        self._ctrl_buf: dict[asyncio.Queue, list[dict]] = {}
        self._ready: dict[str, asyncio.Event] = {}        # stream -> subscribed-on-daemon
        self._desired: set[str] = set()                   # streams to (re)subscribe on connect
        self._last_seq: dict[str, int] = {}               # stream -> highest delivered seq
        self._baseline: dict[str, int | None] = {}        # stream -> initial `since` for first subscribe
        self._epoch: str | None = None                    # daemon PROC_EPOCH; change ⇒ restart
        self._sha: str | None = None                      # daemon's running git sha (from /info)
        self._started = False

    async def call(self, method: str, *, timeout: float | None = None, **params) -> dict:
        """Invoke an RPC. ``timeout`` overrides the client default for this call.

        A wedged/slow daemon surfaces as a typed ``DaemonUnavailable`` — NOT a raw
        ``httpx.ReadTimeout`` bubbling to an unhandled ASGI 500 (the GET /api/accounts
        wedge, incident 2026-07-02). Callers on the control path (usage, counts) pass a
        short timeout so one wedged daemon can't tie up the edge for the full 30s.

        ALWAYS bound a call with this parameter, NEVER with ``asyncio.wait_for`` around
        it. wait_for CANCELS the request rather than timing it out, and a cancelled RPC
        both orphans its socket and strands its pool slot: the spike's controlled
        experiment held everything else fixed and showed httpx's OWN timeout leaked zero
        connections while cancellation leaked one per request, until the pool was full and
        every RPC ``PoolTimeout``d against a healthy daemon (incident 2026-08-23, spike
        docs/superpowers/spikes/2026-08-23-edge-socket-leak.md).

        THAT experiment is the load-bearing evidence; the place inside httpcore where a
        cancel does this is NOT established — see the spike's "Where inside httpcore"
        section. Do not treat it as a TLS-only hazard: the only shape that survives source
        reading (a pooled connection created but never dispatched on) needs no handshake and
        is identical on `uds=`. A bounded attempt failed to reproduce a strand over unix,
        which is not an exemption — so the ban below applies to BOTH branches.

        ``server.sessions.facade.call_with_deadline`` is the helper for call sites that
        must work against both orchestrator implementations.
        """
        post_kwargs = {} if timeout is None else {"timeout": _call_timeout(timeout)}
        try:
            resp = await self._http.post(f"/rpc/{method}", json=params, **post_kwargs)
        except httpx.PoolTimeout as exc:
            # Distinguished from every other timeout because it means something different:
            # the daemon was never asked. The pool had no slot to give within the pool leg,
            # so this RPC failed on OUR side of the wire. That is the wedge, and it is what
            # both incidents spent their diagnosis budget on.
            self._note_pool_timeout(method)
            raise DaemonTimeout(f"{method} timed out: {exc}") from exc
        except httpx.TimeoutException as exc:
            self._note_slot_served()
            raise DaemonTimeout(f"{method} timed out: {exc}") from exc
        except httpx.TransportError as exc:
            # Covers ConnectError, ReadError, WriteError, CloseError — any transport-level
            # failure that means the daemon is unreachable or reset mid-response. The
            # daemon WAS reached (or at least dialled), so the pool is not the problem.
            self._note_slot_served()
            raise DaemonUnavailable(str(exc)) from exc
        self._note_slot_served()
        body = resp.json()
        if "error" in body:
            err = body["error"]
            raise DaemonError(err.get("type", "Error"), err.get("message", ""), err.get("data"))
        return body["result"]

    def _note_slot_served(self) -> None:
        """Record that a request got past the pool, and end any pool-timeout streak.

        Called on EVERY outcome that proves a slot was handed out — a completed RPC, a
        read/write timeout on the wire, a transport reset. All three mean the pool is
        turning over, which is the fact ``_note_pool_timeout`` needs and the counters
        must not contradict.
        """
        self._consecutive_pool_timeouts = 0
        self._last_slot_at = time.monotonic()

    def _pool_is_exhausted(self) -> bool:
        """True when a pool timeout means "nothing is ever freed", not "we are busy".

        The two look IDENTICAL in occupancy — a saturated pool with queued requests is
        what a fan-out burst and a permanent strand both produce — so occupancy cannot
        tell them apart, and reporting on occupancy alone is how ordinary concurrent use
        gets called a wedge. What separates them is turnover: under load the pool hands
        out slots continuously (``_note_slot_served`` fires on every completed, timed-out
        or reset RPC), whereas a pool holding unevictable connections hands out none, ever.
        So: no slot served for ``_WEDGE_QUIET_S`` while we are still failing to get one.

        The reserved ``/info`` channel was the other candidate signal and was NOT used:
        ``_reconcile_epoch`` polls it only once per WS reconnect, so its last result can be
        minutes stale — it is a restart signal, not a liveness feed. Turnover is measured
        by this pool, about this pool, at the moment of the timeout.
        """
        return time.monotonic() - self._last_slot_at >= _WEDGE_QUIET_S

    def _note_pool_timeout(self, method: str) -> None:
        """Record a pool timeout and, when it IS a wedge, make it legible in the journal.

        WARNING, not ERROR-once: the counts are the diagnostic, and an operator needs them
        without shelling in to run ``ss -tn state close-wait``.

        The verdict is gated, deliberately. A pool timeout alone does not mean the pool is
        broken: with ``max_connections`` slots, a ~63-RPC page fan-out and RPCs that can
        legitimately run seconds, concurrent dashboard loads queue behind a *working* pool,
        and calling that "edge-side pool exhaustion" would make the one diagnostic this
        branch asks an operator to trust fire on ordinary use. So the line says what is
        observed (this RPC could not get a slot, here is the occupancy) and only names
        exhaustion when ``_pool_is_exhausted`` — no slot served to anyone for
        ``_WEDGE_QUIET_S`` — actually holds.

        THROTTLED, because during a real wedge EVERY RPC ends here — a dashboard polling
        several routes would otherwise write a warning per failed request per poll and
        bury the very line it is meant to surface. The first timeout of a streak always
        logs (that is the transition an operator wants timestamped); after that, at most
        one line per ``_POOL_WARN_INTERVAL_S``, each carrying the running counts, so a
        standing wedge reads as a steady heartbeat rather than a flood. The counters
        themselves are never throttled — ``pool_stats()`` stays exact.
        """
        self._pool_timeouts += 1
        self._consecutive_pool_timeouts += 1
        now = time.monotonic()
        first_of_streak = self._consecutive_pool_timeouts == 1
        if not first_of_streak and now - self._last_pool_warn_at < _POOL_WARN_INTERVAL_S:
            return
        self._last_pool_warn_at = now
        stats = self.pool_stats()
        verdict = (
            "No connection has been served to any caller for %.0fs, so this is edge-side "
            "pool exhaustion — the daemon was never asked." % stats["secondsSinceSlot"]
            if stats["exhausted"] else
            "The pool served a caller %.1fs ago, so it is still turning over: this is "
            "queueing under load, not exhaustion." % stats["secondsSinceSlot"]
        )
        log.warning(
            "ipc: RPC %s could not get a connection slot (%d consecutive pool timeout(s), "
            "%d total) — pool %d/%d connections, %d in use, %d idle, %d request(s) queued. "
            "%s",
            method, self._consecutive_pool_timeouts, self._pool_timeouts,
            stats["connections"], stats["maxConnections"], stats["inUse"], stats["idle"],
            stats["queued"], verdict)

    def pool_stats(self) -> dict:
        """A JSON-safe snapshot of the RPC connection pool.

        Counts come from httpcore's private ``_pool``, so every field degrades to ``-1``
        rather than raising — this is telemetry and must never be able to fail a health
        route. (Same guard, and the same private accessor, as ``_recycle_transport``;
        there is deliberately only one way into the pool internals in this module.)

        ``inUse`` is the load-bearing number: a connection that is not *available* is
        holding one of ``maxConnections`` slots, and httpcore can only ever evict a
        connection that is closed, expired, or surplus-IDLE.

        ``saturated`` (``inUse == maxConnections``) is a SHAPE, not a verdict — a busy
        fan-out reaches it and recovers. ``exhausted`` is the verdict: saturated *and*
        the pool has served nobody for ``_WEDGE_QUIET_S`` (``secondsSinceSlot``), which
        is what a strand looks like and a burst never does. Read ``exhausted``, not
        ``saturated``, before concluding the edge is wedged.
        """
        since_slot = time.monotonic() - self._last_slot_at
        stats = {
            # The CONFIGURED ceiling (module-level `_LIMITS`), not a read of the
            # transport's own limit. They cannot diverge in production — `_LIMITS` is a
            # constant — but this is the number to change if limits ever go per-client.
            "maxConnections": _LIMITS.max_connections,
            "connections": -1, "inUse": -1, "idle": -1, "queued": -1,
            "poolTimeouts": self._pool_timeouts,
            "consecutivePoolTimeouts": self._consecutive_pool_timeouts,
            "secondsSinceSlot": round(since_slot, 3),
            "saturated": False,
            "exhausted": False,
        }
        try:
            pool = self._transport._pool
            conns = list(pool.connections)
            stats["connections"] = len(conns)
            stats["inUse"] = sum(1 for c in conns if not c.is_available())
            stats["idle"] = sum(1 for c in conns if c.is_idle())
            stats["queued"] = sum(1 for r in pool._requests if r.is_queued())
            stats["saturated"] = bool(
                _LIMITS.max_connections is not None
                and stats["inUse"] >= _LIMITS.max_connections)
            # Both conditions, deliberately: a saturated pool that is still handing out
            # slots is load, and a quiet pool with free slots is just an idle edge.
            stats["exhausted"] = bool(stats["saturated"] and self._pool_is_exhausted())
        except Exception:  # noqa: BLE001 — telemetry only; never fail a health route
            pass
        return stats

    async def info(self) -> dict:
        """GET ``/info`` over the RESERVED health channel (see ``_INFO_LIMITS``).

        Deliberately NOT on ``self._http``: this is the call that carries the restart
        signal, and it must still get through when every RPC slot is pinned ACTIVE.
        Failures are mapped to the module's own typed exceptions — a raw
        ``httpx.ReadTimeout`` used to escape ``except DaemonUnavailable`` handlers here
        (``automations/archive_audit.py``'s pre-flight is one) because only
        ``ConnectError`` was caught.

        A ``PoolTimeout`` here recycles the channel and retries ONCE. The reserved pool
        holds a single connection, so one stranded connection wedges it permanently — and
        that failure is circular and silent: ``/info`` times out, ``_reconcile_epoch``
        swallows it, ``_recycle_transport`` never runs, and the only thing that could
        clear the strand is a *successful* ``/info``. ``pool_stats()`` reports the RPC
        pool, so nothing would show it either.

        This is not the "recycle on PoolTimeout" trigger that was rejected for the RPC
        pool, and the objection to that one does not transfer: this pool has exactly ONE
        serial caller, so contention is impossible by construction and a pool timeout on
        it is unambiguously a strand. The blast radius is one health socket, not ~100
        in-flight RPCs. It is defence in depth — no cancellation vector reaches this
        channel today — but it is the last path on which the self-heal could fail closed
        and stay failed.
        """
        for attempt in (1, 2):
            try:
                resp = await self._info_http.get("/info")
            except httpx.PoolTimeout as exc:
                if attempt == 2:
                    raise DaemonTimeout(f"/info timed out: {exc}") from exc
                log.warning(
                    "ipc: the reserved /info channel could not get its connection slot — "
                    "recycling it and retrying once. A single-connection pool that is "
                    "starved is a stranded socket, not contention: this channel has one "
                    "serial caller.")
                try:
                    await self._info_transport.aclose()
                except Exception as exc2:  # noqa: BLE001 — retry anyway; a fresh
                    # connection may still be obtainable, and raising here would restore
                    # exactly the permanent-wedge shape this recycle exists to prevent.
                    log.warning("ipc: /info channel recycle failed (%s) — retrying anyway",
                                exc2)
            except httpx.TimeoutException as exc:
                raise DaemonTimeout(f"/info timed out: {exc}") from exc
            except httpx.TransportError as exc:
                raise DaemonUnavailable(str(exc)) from exc
            else:
                return resp.json()
        raise AssertionError("unreachable")          # pragma: no cover

    @property
    def epoch(self) -> str | None:
        """The daemon's current process epoch (from ``/info``), or ``None`` when the edge
        has not yet reached ``/info`` (reader not connected). Changes on a daemon restart.

        The edge reads this — through RemoteOrchestrator.daemon_epoch — to reject a stale
        cross-epoch catch-up cursor (#497): a browser's srcSeq cursor is only meaningful
        within the epoch that minted it, so after a restart (seq space resets to 0) a stale
        high cursor must force a full replay, never a catch-up against the fresh low ring.
        Prefer this public accessor over reaching into ``_epoch``."""
        return self._epoch

    @property
    def sha(self) -> str | None:
        """The daemon's running git sha (from ``/info``), or ``None`` before first contact.

        Cached from the same ``/info`` call that tracks the epoch. The deploy gate
        compares it against the deployed sha: an edge on a new commit talking to a
        daemon on an old one is the #719 failure (a new RPC caller whose handler
        does not exist yet), and it is otherwise invisible — the edge stays healthy."""
        return self._sha

    def _add_subscriber(self, stream: str, maxsize: int = _BASE_Q_SIZE) -> asyncio.Queue:
        """Register a new local subscriber queue for *stream* and return it."""
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subscribers.setdefault(stream, set()).add(q)
        self._dropped[q] = 0
        self._ctrl_buf[q] = []
        return q

    def subscribe(self, stream: str, since: int | None = None, maxsize: int | None = None):
        if maxsize is None:
            # Auto-scale: one extra base-size bucket per four already-open streams so
            # the queue keeps up under concurrent-session automation load without the
            # caller having to know anything about session count.
            n_active = len(self._subscribers)
            maxsize = _BASE_Q_SIZE * max(1, n_active // 4)
        q = self._add_subscriber(stream, maxsize)
        self._ready.setdefault(stream, asyncio.Event())
        # The daemon-side subscription is per stream, not per local subscriber. Only the
        # FIRST subscriber establishes (and seeds the resume baseline for) the stream;
        # additional subscribers just join the fan-out. Re-sending subscribe for an
        # already-desired stream would re-trigger the daemon's replay-on-subscribe and
        # duplicate events into every queue.
        newly_desired = stream not in self._desired
        if newly_desired:
            self._desired.add(stream)
            self._baseline[stream] = since
        if not self._started:
            self._started = True
            self._reader_task = asyncio.create_task(self._reader())
        elif self._ws is not None and newly_desired:
            # Reader already connected — register this new stream now. (On the first
            # subscribe, or after a reconnect, the reader subscribes all _desired itself.)
            asyncio.create_task(self._send_subscribe(stream))
        return self._iter(stream, q)

    def _since_for(self, stream: str) -> int | None:
        # Reconnect → resume after the last delivered seq; first subscribe → the baseline.
        if stream in self._last_seq:
            return self._last_seq[stream]
        return self._baseline.get(stream)

    async def _recycle_transport(self, was: str, now: str) -> None:
        """Discard every pooled connection. Called ONLY when the daemon has restarted.

        WHAT THIS DOES: closes the httpcore pool (``AsyncHTTPTransport.aclose()`` →
        ``AsyncConnectionPool.aclose()``), which drops every connection — including ones
        pinned ACTIVE — and leaves the pool ready to build fresh ones. httpcore 1.0.9's
        pool has no "closed" flag; ``aclose`` clears ``_connections`` and nothing else, so
        the very next request reconnects.

        WHY NOT the alternatives:
        - *Sweep idle connections.* Useless here, for EITHER strand shape. httpcore only
          evicts connections that are closed, expired, or surplus-IDLE; a connection stuck
          ACTIVE reports none of those, and neither does one the pool created but never
          dispatched on (``_connection is None``, ``_connect_failed`` False → is_closed,
          has_expired, is_idle and is_available are all False). That is exactly why nothing
          reclaimed them for the life of the process, and why a close-idle implementation
          would pass a naive test and fix nothing.
        - *``self._http.aclose()``.* httpx flips the client to CLOSED permanently; every
          later request raises ``RuntimeError``. That turns a wedge into a hard outage.
        - *Build a new AsyncClient and swap it in.* Callers already inside ``call`` hold
          the old object, so its pool would never be closed — the leak simply moves.

        THE COST, ACCEPTED DELIBERATELY: closing the pool errors requests that are still
        in flight. Most of them were already doomed — opened before the restart, they point
        at a process that no longer exists and can only end in a timeout. But NOT all of
        them, and the difference matters: the reader learns of a restart only on its next
        reconnect (`_reader`'s backoff caps at 2s), so an RPC that opened a FRESH, HEALTHY
        connection to the NEW daemon inside that window is killed as collateral. Concretely:
        a deploy restarts the daemon; 0.5s after it is back an ``automation_*`` RPC (90s
        deadline) connects successfully to the new process; the reconcile that follows tears
        it down. The tradeoff is still right — the window is bounded by the reconnect
        backoff, so the blast radius is a handful of requests, and they DEGRADE rather than
        500: every killed request surfaces as ``httpx.TransportError`` →
        ``DaemonUnavailable``, the typed failure every RPC call site already handles
        (``_enrich_batch`` maps it to empty fields through ``gather(return_exceptions=True)``),
        so a caller sees a fast empty field instead of a 30s hang.

        Never raises: a client that cannot recycle must still be a working client.
        """
        # Logged at WARNING on purpose: a daemon restart is rare and operationally
        # significant, and both wedges were diagnosed by hand with `ss` because the
        # journal said nothing about the pool. The count comes from a private httpcore
        # attribute, so it degrades to -1 rather than ever pre-empting the recycle.
        stats = self.pool_stats()
        log.warning("ipc: daemon restarted (epoch %s → %s) — recycling %d pooled "
                    "connection(s) to the old process (%d in use, %d idle, %d queued, "
                    "%d pool timeout(s) since start)", was, now, stats["connections"],
                    stats["inUse"], stats["idle"], stats["queued"], stats["poolTimeouts"])
        # The wedge is over as far as the counters are concerned: the slots are about to be
        # returned, so a stale consecutive count must not keep reading as an active wedge —
        # and the quiet-window clock must restart too. `_note_slot_served` bundles BOTH,
        # which is exactly why this site calls it instead of resetting one by hand: leaving
        # `_last_slot_at` stale meant a burst that saturated the FRESH pool was measured
        # against a timestamp from before the restart, so it read as
        # `secondsSinceSlot > _WEDGE_QUIET_S` and reported exhaustion. A false wedge alarm
        # in the seconds after a restart is the worst possible moment for one: that is
        # precisely when an operator is reading these logs.
        self._note_slot_served()
        # The reserved health channel's own connection also points at the dead process.
        # Recycled separately and first, so a failure here can never skip the RPC pool
        # recycle — that is the one that clears the outage.
        try:
            await self._info_transport.aclose()
        except Exception as exc:  # noqa: BLE001 — the health channel self-heals next poll
            log.warning("ipc: health-channel recycle failed (%s)", exc)
        try:
            await self._transport.aclose()
        except Exception as exc:  # noqa: BLE001 — a failed recycle must not brick the client
            # aclose() empties `_connections` before closing them, so even a partial
            # failure leaves the pool usable — just with a socket or two still held.
            log.warning("ipc: pool recycle after daemon restart failed (%s) — "
                        "connections to the old process may linger", exc)

    async def _reconcile_epoch(self) -> None:
        """Detect a daemon restart, recycle the HTTP pool, and drop stale watermarks.

        The daemon's seq space is per-process: a restart resets seq→0. Carrying the
        old high `since` into the new daemon makes its `seq > since` replay filter drop
        every restart-window event. On a changed epoch, clear `_last_seq` and reset each
        baseline to None so the next subscribe is `since=None` (full-ring replay).
        Best-effort: if `/info` is unreachable, keep prior watermarks and retry next loop.

        A changed epoch means every pooled HTTP connection is dead BY DEFINITION — they
        point at a process that no longer exists — so this is also where the transport is
        recycled (issue #734). Without it, connections stranded by a build that
        still cancelled RPCs are never reclaimed, the pool fills to `max_connections`, and
        every RPC-backed route 503s until someone restarts the edge by hand. That made
        DEPLOYING a fix break the app: the deploy restarts the daemon, which is the very
        event that wedged the edge.

        The restart signal comes from `/info`, which rides its OWN one-connection pool
        (`_INFO_LIMITS`) precisely so this can still fire when the RPC pool is already
        saturated — the state both outages ended in, where the recycle was needed most and
        was previously starved out. Recycling on a pool timeout instead was considered and
        rejected: a legitimate fan-out that saturates the pool under a slow daemon would
        then kill 100 healthy in-flight requests, a self-inflicted outage rather than a
        self-heal. Isolating `/info` gets the signal through without making pool pressure
        itself the trigger.

        THE BOUND ON THAT GUARANTEE: this runs from the events reader, and the reader task
        is created only on the first `subscribe()`. On an edge with NO live WS subscriber
        it therefore never runs at all — enrichment RPCs still flow, but no restart is
        ever reconciled and no pool is ever recycled. The reserved channel removes the
        *starvation*; it does not make the recycle unconditional. That is tolerable only
        because the strand source itself was removed (RPCs are no longer cancelled), so a
        subscriber-less edge should not be accumulating strands to begin with. Do not
        build anything on an assumption that this fires periodically — it does not.
        """
        try:
            info = await self.info()
        except Exception:  # noqa: BLE001 — /info momentarily unavailable mid-restart
            return
        # Same response already carries the daemon's running sha; cache it so the
        # edge can report it on /healthz and the deploy gate can catch edge/daemon
        # version skew without a second round-trip.
        self._sha = info.get("sha") or self._sha
        epoch = info.get("epoch")
        if epoch is None:
            return
        if self._epoch is not None and epoch != self._epoch:
            await self._recycle_transport(self._epoch, epoch)
            self._last_seq.clear()
            for stream in self._baseline:
                self._baseline[stream] = None
        self._epoch = epoch

    async def _send_subscribe(self, stream: str) -> None:
        ws = self._ws
        if ws is None:
            return  # a reconnect will re-subscribe all _desired
        try:
            await ws.send(json.dumps(proto.subscribe_frame(stream, since=self._since_for(stream))))
            self._ready[stream].set()
        except Exception:  # noqa: BLE001 — reader re-subscribes on its next connect
            pass

    async def events_ready(self, stream: str) -> None:
        """Await until the daemon has acknowledged the subscription (test sync point)."""
        await asyncio.wait_for(self._ready[stream].wait(), 5)

    async def _iter(self, stream: str, q: asyncio.Queue):
        try:
            while True:
                lost = self._dropped.get(q, 0)
                if lost:
                    # This queue overflowed since we last drained it: emit ONE GAP for
                    # the whole burst (not one per dropped event), then reset. Same wire
                    # shape as the daemon ring-eviction GAP so ws._forward_live's existing
                    # branch forces a from-disk resync. oldest=None: a local drop has no
                    # daemon seq anchor. seq is untouched — a gap is not a real event.
                    self._dropped[q] = 0
                    yield {"type": proto.GAP, "stream": stream, "lost": lost, "oldest": None}
                event = await q.get()
                # A slot just freed. Re-inject any buffered control events so they
                # enter the queue in their original arrival order, ahead of future
                # transcript events, and are delivered without further delay.
                ctrl_buf = self._ctrl_buf.get(q)
                if ctrl_buf:
                    while ctrl_buf and not q.full():
                        q.put_nowait(ctrl_buf.pop(0))
                yield event
        finally:
            # Drop just THIS subscriber's queue; other subscribers on the stream keep
            # receiving. (GeneratorExit/cancel of a consuming task lands here.)
            subs = self._subscribers.get(stream)
            if subs is not None:
                subs.discard(q)
            self._dropped.pop(q, None)
            self._ctrl_buf.pop(q, None)

    def _deliver(self, stream: str, event: dict, seq: int | None = None) -> None:
        if seq is not None:
            self._last_seq[stream] = seq
        subs = self._subscribers.get(stream)
        if not subs:
            return
        # Thread the daemon's source seq onto the delivered event WITHOUT changing its
        # content (a dict subclass — see _SeqDict): the client's dedup/heal key is the
        # per-attach seq stamped later by ws._forward_live, not this. Wrapped once and
        # fanned out (all subscribers on a stream see the same source seq).
        event = _SeqDict(event, seq)
        for q in subs:
            if q.full():
                if event.get("type") in _CTRL_EVENT_TYPES:
                    # Control events must never be evicted: stash in a side buffer
                    # that _iter re-injects as slots free, preserving arrival order.
                    self._ctrl_buf.setdefault(q, []).append(event)
                    log.warning(
                        "ipc: subscriber queue for %s full — buffering control event %s "
                        "(queue=%d, ctrl_buf=%d)",
                        stream, event.get("type"), q.maxsize,
                        len(self._ctrl_buf[q]),
                    )
                else:
                    try:
                        q.get_nowait()  # drop oldest transcript event
                    except asyncio.QueueEmpty:
                        pass
                    # Count the drop; _iter turns a non-zero count into one GAP for
                    # the consumer so the lost span is visible instead of vanishing.
                    self._dropped[q] = self._dropped.get(q, 0) + 1
                    log.warning(
                        "ipc: subscriber queue for %s full — dropped oldest (%d lost, GAP pending)",
                        stream, self._dropped[q],
                    )
                    q.put_nowait(event)
            else:
                q.put_nowait(event)

    async def _reader(self) -> None:
        backoff = 0.05
        while True:
            try:
                async with daemon_ws_connect(self.endpoint, "/events", self._ssl) as ws:
                    self._ws = ws
                    backoff = 0.05
                    # A reconnect may be a brand-new daemon (deploy/crash) whose seq
                    # space reset to 0. Reconcile the epoch BEFORE resubscribing so a
                    # stale `since` watermark can't make the new daemon skip every
                    # restart-window event (the silent transcript-tail gap).
                    await self._reconcile_epoch()
                    for stream in list(self._desired):
                        await ws.send(json.dumps(proto.subscribe_frame(stream, since=self._since_for(stream))))
                        self._ready[stream].set()
                    async for raw in ws:
                        frame = json.loads(raw)
                        ftype = frame.get("type")
                        if ftype == proto.EVENT:
                            self._deliver(frame["stream"], frame["event"], frame.get("seq"))
                        elif ftype == proto.GAP:
                            # The daemon's in-memory replay couldn't cover our resume anchor
                            # (issue #185): `lost` events were evicted before we reconnected.
                            # Surface it loudly — the in-session view for this stream is now
                            # behind durable storage and should be resynced from the transcript.
                            stream = frame.get("stream")
                            log.warning(
                                "ipc: replay gap on %s — %s event(s) lost before seq %s; "
                                "in-memory view is incomplete (resync from disk)",
                                stream, frame.get("lost"), frame.get("oldest"))
                            # Propagate the gap to local subscribers as a control event so
                            # the consumer (RemoteDriver → ws._forward_live) can trigger a
                            # from-disk resync. Logging alone left the edge view silently
                            # behind durable storage. seq=None: a gap is not a real event, so
                            # it must NOT advance the resume watermark.
                            self._deliver(stream, {
                                "type": proto.GAP, "stream": stream,
                                "lost": frame.get("lost"), "oldest": frame.get("oldest"),
                            })
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — drop/connect failures → retry
                log.warning("ipc: events WS error (%s) — reconnecting in %.2fs", exc, backoff)
            self._ws = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 2.0)

    async def aclose(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # The reserved health channel is a second AsyncClient, so it needs its own close
        # or its socket outlives the client (short-lived callers like host_probe build one
        # DaemonClient per probe). Closed FIRST and each close guarded independently: with
        # the RPC close first and unguarded, a raise there skipped the health-channel close
        # entirely and leaked the very socket this channel added.
        for closer in (self._info_http.aclose, self._http.aclose):
            try:
                await closer()
            except Exception:  # noqa: BLE001 — best-effort teardown; close them both
                log.debug("ipc: client teardown failed on one channel", exc_info=True)
