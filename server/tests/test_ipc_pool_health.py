"""A wedged RPC pool must be VISIBLE — no `ss -tn state close-wait` required (issue #734).

Both edge outages had the same diagnostic shape: every daemon-backed route 503'd after
exactly the pool timeout, the journal said only that individual RPCs failed, and the
actual cause — all `max_connections` slots held by connections the pool could not evict —
was found by hand with a socket dump. The system knew; it just never said.

These tests pin the three things that fix that:
  1. ``DaemonClient.pool_stats()`` reports the occupancy that distinguishes "the daemon is
     down" from "we cannot reach a healthy daemon because our own pool is full".
  2. A pool timeout is counted and logged at WARNING with that occupancy attached, so the
     wedge is legible in the journal as it happens.
  3. That verdict fires ONLY on a wedge. Occupancy alone cannot tell a strand from a busy
     fan-out — both saturate the pool and queue requests behind it — so a diagnostic keyed
     on occupancy would cry wedge during ordinary concurrent use, and an operator who has
     seen it do that will not believe it when it matters.
and that the numbers reach an operator through the existing ``/api/daemon/health``
surface rather than a new endpoint.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import types
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from server.app import create_app
from server.ipc import client as ipc_client
from server.ipc.client import DaemonClient, DaemonTimeout
from server.ipc.daemon import create_daemon_app, register_rpc
from server.ipc.tls import ClientTLSFiles
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory
from server.tests.ipc_harness import serve_daemon_tls

_SETTLE = 5.0
_IDENT = {"Tailscale-User-Login": "health@example.com"}

# The budget a deliberately-starved test call declares. The module's pool leg outlasts the
# slowest real RPC (`_POOL_ACQUIRE_S`) so a fan-out queue is served rather than shed, and
# `_call_timeout` gives a caller its WHOLE budget on the slot — so a test that wants a fast
# PoolTimeout has to ask for a short budget rather than rely on the shared leg being short.
_STARVE_BUDGET = 0.3


# --------------------------------------------------------------------------- #
# Harness — a real daemon with an RPC that never answers, so connections pin ACTIVE.
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def daemon(mtls_certs):
    app = create_daemon_app()
    state = types.SimpleNamespace(release=asyncio.Event(), started=0)

    async def _hang(params: dict) -> dict:
        state.started += 1
        try:
            await asyncio.wait_for(state.release.wait(), 20)
        except asyncio.TimeoutError:
            pass
        return {"released": True}

    async def _slow(params: dict) -> dict:
        """A healthy-but-slow RPC. transcript_events was measured at 4.3s in production,
        so a pool slot legitimately being held for a while is normal load, not a strand."""
        state.slow_started += 1
        await asyncio.sleep(float(params.get("secs", 0.5)))
        return {"slow": True}

    state.slow_started = 0
    register_rpc(app, "hang", _hang)
    register_rpc(app, "slow", _slow)
    async with serve_daemon_tls(app, mtls_certs) as endpoint:
        state.endpoint = endpoint
        try:
            yield state
        finally:
            state.release.set()
            await asyncio.sleep(0.05)


def _client(daemon, certs, *, max_connections: int, monkeypatch) -> DaemonClient:
    """A DaemonClient with a small pool, built through the module's own ``_LIMITS``.

    Patched rather than passed so the client is still constructed by the single code path
    under test — and so ``pool_stats()`` reports the same ceiling the pool is enforcing.
    """
    monkeypatch.setattr(ipc_client, "_LIMITS", httpx.Limits(
        max_connections=max_connections, max_keepalive_connections=max_connections,
        keepalive_expiry=5.0))
    tls = ClientTLSFiles(cert=certs.client_cert, key=certs.client_key, ca=certs.ca)
    return DaemonClient(daemon.endpoint, client_tls=tls)


async def _saturate(client: DaemonClient, daemon, n: int):
    tasks = [asyncio.create_task(client.call("hang")) for _ in range(n)]
    deadline = time.monotonic() + _SETTLE
    while time.monotonic() < deadline and daemon.started < n:
        await asyncio.sleep(0.02)
    assert daemon.started == n, f"only {daemon.started}/{n} RPCs reached the daemon"
    return tasks


# --------------------------------------------------------------------------- #
# 1. The occupancy numbers themselves
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pool_stats_reports_a_healthy_idle_pool(daemon, mtls_certs, monkeypatch):
    """Steady state: a connection was used, is now idle, and holds no slot."""
    client = _client(daemon, mtls_certs, max_connections=4, monkeypatch=monkeypatch)
    try:
        await client.call("echo", stream="s", text="warm")
        stats = client.pool_stats()
        assert stats["maxConnections"] == 4
        assert stats["connections"] >= 1
        assert stats["idle"] >= 1
        assert stats["inUse"] == 0, "an answered RPC is still holding a slot"
        assert stats["queued"] == 0
        assert stats["saturated"] is False
        assert stats["poolTimeouts"] == 0
        assert stats["consecutivePoolTimeouts"] == 0
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_pool_stats_reports_the_wedge(daemon, mtls_certs, monkeypatch):
    """The signature that mattered: every slot in use against a HEALTHY daemon.

    ``inUse`` is what separates this from "the daemon is down" — the distinction that cost
    both outages their diagnosis time. httpcore can only evict a connection that is closed,
    expired, or surplus-IDLE, so a pool at ``inUse == maxConnections`` cannot recover on
    its own *if nothing in it will ever complete*.

    That caveat is why ``saturated`` is a shape, not a verdict: a busy fan-out reaches the
    same occupancy and recovers (see ``test_a_fan_out_burst_is_not_reported_as_exhaustion``).
    ``exhausted`` is the verdict, and it needs turnover to have stopped as well.
    """
    client = _client(daemon, mtls_certs, max_connections=2, monkeypatch=monkeypatch)
    try:
        inflight = await _saturate(client, daemon, 2)

        stats = client.pool_stats()
        assert stats["inUse"] == 2 == stats["maxConnections"]
        assert stats["idle"] == 0, "a wedged pool must not report reclaimable connections"
        assert stats["saturated"] is True

        # A caller arriving now is queued behind a pool that will not free a slot.
        queued = asyncio.create_task(
            client.call("echo", stream="s", text="wedged", timeout=_STARVE_BUDGET))
        await asyncio.sleep(0.2)
        assert client.pool_stats()["queued"] >= 1, "a starved request is invisible"
        with pytest.raises(DaemonTimeout):
            await queued

        for t in inflight:
            t.cancel()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_pool_stats_degrades_instead_of_raising(daemon, mtls_certs, monkeypatch):
    """The counts come from a private httpcore attribute. If httpcore moves it, health
    must report -1, never 500 — this is telemetry riding on a route an operator needs
    most when things are already broken."""
    class _MovedPool:
        """httpcore after a hypothetical refactor: no ``connections``, no ``_requests``.
        ``aclose`` is kept so teardown still works — the point is the accessor, not close.
        """
        async def aclose(self):
            pass

    client = _client(daemon, mtls_certs, max_connections=2, monkeypatch=monkeypatch)
    try:
        monkeypatch.setattr(client._transport, "_pool", _MovedPool(), raising=False)

        stats = client.pool_stats()
        assert stats["connections"] == stats["inUse"] == stats["idle"] == -1
        assert stats["queued"] == -1
        assert stats["saturated"] is False, "an unknown pool must not be reported wedged"
        assert stats["maxConnections"] == 2, "the configured ceiling is not from httpcore"
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- #
# 2. The journal line
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_pool_timeout_is_counted_and_logged(daemon, mtls_certs, monkeypatch,
                                                    caplog):
    """A pool timeout means the daemon was never asked. That is a different failure from
    every other timeout and must say so, with the occupancy that proves it."""
    client = _client(daemon, mtls_certs, max_connections=1, monkeypatch=monkeypatch)
    try:
        inflight = await _saturate(client, daemon, 1)

        with caplog.at_level(logging.WARNING, logger="server.ipc.client"):
            with pytest.raises(DaemonTimeout):
                await client.call("echo", stream="s", text="starved", timeout=_STARVE_BUDGET)

        assert client.pool_stats()["poolTimeouts"] == 1
        assert client.pool_stats()["consecutivePoolTimeouts"] == 1
        warned = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("connection slot" in m and "pool timeout" in m for m in warned), warned
        assert any("echo" in m for m in warned), "the starved RPC is not named"

        for t in inflight:
            t.cancel()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_a_fan_out_queue_is_served_not_shed(daemon, mtls_certs, monkeypatch):
    """Queueing behind a SLOW BUT HEALTHY RPC must end in a result, not a DaemonTimeout.

    This is the shape production actually runs. GET /api/sessions fans out ~63 RPCs, and
    with `max_connections` slots two concurrent dashboard loads leave a tail of requests
    queued behind slots that are legitimately held for seconds — `transcript_events` was
    measured at 4.3s. A pool leg shorter than that fails the whole tail with empty
    enrichment fields against a daemon that is doing nothing wrong, and raises the
    pool-exhaustion diagnostic while doing it.

    Scaled down: 2 slots, 4 concurrent 2.2s RPCs, so half the burst queues for longer than
    the old 2s leg allowed. Every one of them must still get an answer.
    """
    client = _client(daemon, mtls_certs, max_connections=2, monkeypatch=monkeypatch)
    try:
        results = await asyncio.gather(
            *(client.call("slow", secs=2.2) for _ in range(4)),
            return_exceptions=True)

        failed = [r for r in results if isinstance(r, Exception)]
        assert not failed, (
            f"{len(failed)}/4 queued-but-servable requests were shed by the pool leg: "
            + "; ".join(f"{type(r).__name__}: {r}" for r in failed))
        assert client.pool_stats()["poolTimeouts"] == 0, (
            "ordinary fan-out load was counted as a pool timeout")
        assert daemon.slow_started == 4, "the burst never reached the daemon"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_a_brief_starvation_is_not_yet_called_exhaustion(daemon, mtls_certs,
                                                               monkeypatch, caplog):
    """A pool timeout on its own is NOT evidence of a wedge, and must not be reported as one.

    A saturated pool with queued requests is what a fan-out burst and a permanent strand
    both look like, so occupancy cannot tell them apart. Turnover can: seconds after the
    pool last served somebody, the honest answer is "queued", not "exhausted". Claiming
    exhaustion this early is how the one diagnostic this branch asks an operator to trust
    ends up firing during ordinary concurrent use — and being ignored when it matters.
    """
    client = _client(daemon, mtls_certs, max_connections=1, monkeypatch=monkeypatch)
    try:
        inflight = await _saturate(client, daemon, 1)

        with caplog.at_level(logging.WARNING, logger="server.ipc.client"):
            with pytest.raises(DaemonTimeout):
                await client.call("echo", stream="s", text="starved",
                                  timeout=_STARVE_BUDGET)

        warned = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert warned, "a starved request must still be visible in the journal"
        assert not any("pool exhaustion" in m for m in warned), (
            "a pool that was serving callers a moment ago was declared exhausted:\n"
            + "\n".join(warned))
        assert any("queueing under load" in m for m in warned), warned
        assert client.pool_stats()["exhausted"] is False

        for t in inflight:
            t.cancel()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_a_pool_that_serves_nobody_is_reported_as_exhaustion(daemon, mtls_certs,
                                                                   monkeypatch, caplog):
    """The other half: the verdict must still fire when it IS the wedge.

    Gating the wording is only correct if a genuine strand — every slot held by a
    connection nothing will ever reclaim, no caller served at all — still says so. The
    distinguishing signal is turnover, so the test drives it directly: saturate, then let
    the quiet window elapse with nothing completing.
    """
    monkeypatch.setattr(ipc_client, "_WEDGE_QUIET_S", 0.3)
    client = _client(daemon, mtls_certs, max_connections=1, monkeypatch=monkeypatch)
    try:
        inflight = await _saturate(client, daemon, 1)
        await asyncio.sleep(0.35)                    # no slot served for the whole window

        with caplog.at_level(logging.WARNING, logger="server.ipc.client"):
            with pytest.raises(DaemonTimeout):
                await client.call("echo", stream="s", text="starved", timeout=_STARVE_BUDGET)

        warned = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("pool exhaustion" in m for m in warned), warned
        assert client.pool_stats()["exhausted"] is True

        for t in inflight:
            t.cancel()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_a_completed_rpc_clears_the_consecutive_count(daemon, mtls_certs,
                                                            monkeypatch):
    """``consecutivePoolTimeouts`` is the wedge indicator, so it must mean "right now".
    One starved request during a fan-out burst is not a wedge; a count that never resets
    would read as one forever."""
    client = _client(daemon, mtls_certs, max_connections=1, monkeypatch=monkeypatch)
    try:
        inflight = await _saturate(client, daemon, 1)
        with pytest.raises(DaemonTimeout):
            await client.call("echo", stream="s", text="starved", timeout=_STARVE_BUDGET)
        assert client.pool_stats()["consecutivePoolTimeouts"] == 1

        daemon.release.set()                          # the pool drains
        await asyncio.gather(*inflight, return_exceptions=True)

        assert await client.call("echo", stream="s", text="ok") == {"ok": True}
        stats = client.pool_stats()
        assert stats["consecutivePoolTimeouts"] == 0, "a healthy pool still reads as wedged"
        assert stats["poolTimeouts"] == 1, "the lifetime total must not be reset"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_a_standing_wedge_does_not_flood_the_journal(daemon, mtls_certs, monkeypatch,
                                                           caplog):
    """During a real wedge EVERY RPC pool-timeouts. Unthrottled, a polling dashboard would
    write a warning per failed request per poll and bury the line an operator is looking
    for. The first timeout of a streak always logs; the rest are rate-limited — but the
    COUNTERS stay exact, because they are the diagnostic.
    """
    client = _client(daemon, mtls_certs, max_connections=1, monkeypatch=monkeypatch)
    try:
        inflight = await _saturate(client, daemon, 1)

        with caplog.at_level(logging.WARNING, logger="server.ipc.client"):
            for _ in range(4):
                with pytest.raises(DaemonTimeout):
                    await client.call("echo", stream="s", text="starved", timeout=_STARVE_BUDGET)

        slot_warnings = [r for r in caplog.records if "connection slot" in r.getMessage()]
        assert len(slot_warnings) == 1, (
            f"{len(slot_warnings)} warnings for one standing wedge — not throttled")
        stats = client.pool_stats()
        assert stats["poolTimeouts"] == 4, "throttling must not lose a count"
        assert stats["consecutivePoolTimeouts"] == 4

        for t in inflight:
            t.cancel()
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- #
# 3. The reserved health channel must be able to heal ITSELF
# --------------------------------------------------------------------------- #


def _info_pool(client: DaemonClient):
    """The one-connection pool behind the reserved /info channel."""
    return client._info_transport._pool


async def _strand_the_health_channel(client: DaemonClient, daemon):
    """Pin the health channel's single connection ACTIVE.

    Done exactly the way a cancelled RPC used to strand an RPC connection: a genuinely
    in-flight request that never answers. The pool cannot tell that apart from a strand —
    which is the whole reason nothing could ever reclaim one.
    """
    task = asyncio.create_task(client._info_http.post("/rpc/hang", json={}))
    deadline = time.monotonic() + _SETTLE
    while time.monotonic() < deadline and daemon.started < 1:
        await asyncio.sleep(0.02)
    assert daemon.started == 1, "the strand request never reached the daemon"
    conns = list(_info_pool(client).connections)
    assert len(conns) == 1, f"expected the channel's one connection, got {len(conns)}"
    assert not conns[0].is_available(), "the connection is not holding the only slot"
    assert not task.done(), "the daemon answered — nothing is stranded"
    return conns[0], task


@pytest.mark.asyncio
async def test_a_stranded_health_channel_recovers(daemon, mtls_certs, monkeypatch):
    """One stranded connection must not wedge the health channel for the process lifetime.

    The reserved pool holds a SINGLE connection, so a single strand saturates it — and the
    resulting failure is circular and silent: ``/info`` PoolTimeouts, ``_reconcile_epoch``
    swallows it, ``_recycle_transport`` never runs, and the only thing that could clear the
    strand is a *successful* ``/info``. ``pool_stats()`` reports the RPC pool, so nothing
    surfaces it either. That is the last path on which the whole self-heal fails closed and
    stays failed, so ``info()`` recycles the channel and retries once.

    Safe here in a way it is not for the RPC pool: this pool has exactly ONE serial caller,
    so a pool timeout on it cannot be contention — it is unambiguously a strand.
    """
    client = _client(daemon, mtls_certs, max_connections=4, monkeypatch=monkeypatch)
    strand = None
    try:
        pinned, strand = await _strand_the_health_channel(client, daemon)

        # Before the recycle-and-retry this raised DaemonTimeout, and kept raising it.
        info = await client.info()
        assert info.get("epoch"), f"/info returned no epoch: {info}"

        assert pinned.is_closed(), "the stranded connection kept its socket"
        assert pinned not in _info_pool(client).connections, (
            "the stranded connection still holds the channel's only slot")

        # ...and the channel is healthy from here on, not merely unstuck once.
        assert (await client.info()).get("epoch")
    finally:
        if strand is not None:
            strand.cancel()
            await asyncio.gather(strand, return_exceptions=True)
        await client.aclose()


@pytest.mark.asyncio
async def test_the_health_channel_retry_is_bounded(daemon, mtls_certs, monkeypatch):
    """The retry is ONE retry. A channel that stays starved must surface a typed failure,
    not spin — ``_reconcile_epoch`` runs on the reader's reconnect path and cannot block."""
    client = _client(daemon, mtls_certs, max_connections=4, monkeypatch=monkeypatch)
    strand = None
    try:
        _pinned, strand = await _strand_the_health_channel(client, daemon)

        # The recycle cannot free the slot (simulating a close that does not land), so the
        # retry hits the same PoolTimeout.
        attempts = 0

        async def _noop_recycle():
            nonlocal attempts
            attempts += 1

        monkeypatch.setattr(client._info_transport, "aclose", _noop_recycle)

        t0 = time.monotonic()
        with pytest.raises(DaemonTimeout):
            await client.info()
        assert attempts == 1, f"expected exactly one recycle+retry, got {attempts}"
        assert time.monotonic() - t0 < 3 * _SETTLE, "the bounded retry is not bounded"
    finally:
        if strand is not None:
            strand.cancel()
            await asyncio.gather(strand, return_exceptions=True)
        await client.aclose()


@pytest.mark.asyncio
async def test_aclose_closes_both_channels_even_if_one_raises(daemon, mtls_certs,
                                                              monkeypatch):
    """Teardown must not leak a channel because the other one failed to close.

    With the RPC close first and unguarded, a raise there skipped the health-channel close
    entirely — leaking the very socket the reserved channel added, once per short-lived
    ``DaemonClient`` (``sessions/host_probe.py`` builds one per probe).
    """
    for victim in ("_http", "_info_http"):
        client = _client(daemon, mtls_certs, max_connections=2, monkeypatch=monkeypatch)
        await client.call("echo", stream="s", text="warm")

        async def _explode():
            raise RuntimeError("close blew up")

        monkeypatch.setattr(getattr(client, victim), "aclose", _explode)

        await client.aclose()                        # must not raise

        survivor = "_info_http" if victim == "_http" else "_http"
        assert getattr(client, survivor).is_closed, (
            f"{survivor} was leaked because {victim}.aclose() raised")
        monkeypatch.undo()


# --------------------------------------------------------------------------- #
# 4. It reaches an operator — through the surface that already exists
# --------------------------------------------------------------------------- #


def _edge(tmp_path):
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    app = create_app(orchestrator=orch)
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    return app, patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url})


def test_daemon_health_surfaces_the_pool(tmp_path):
    """``/api/daemon/health`` — the existing wedge-visibility surface (#434) — carries it.

    Deliberately not a new endpoint, and deliberately not an RPC-backed field: it has to
    answer while every RPC is timing out, which is the only time anyone reads it.
    """
    app, ctx = _edge(tmp_path)
    with ctx, TestClient(app, raise_server_exceptions=False) as client:
        app.state.sessions = types.SimpleNamespace(daemon_pool_stats=lambda: {
            "maxConnections": 100, "connections": 100, "inUse": 100, "idle": 0,
            "queued": 7, "poolTimeouts": 42, "consecutivePoolTimeouts": 12,
            "saturated": True,
        })
        body = client.get("/api/daemon/health", headers=_IDENT).json()
        assert body["pool"]["saturated"] is True
        assert body["pool"]["inUse"] == 100
        assert body["pool"]["queued"] == 7
        assert body["pool"]["consecutivePoolTimeouts"] == 12
        # The lifecycle fields it already reported are untouched.
        assert "restartCount24h" in body and "epoch" in body


def test_daemon_health_reports_no_pool_in_process(tmp_path):
    """In-process mode has no daemon and therefore no RPC pool. ``None`` says
    "not applicable" rather than reporting a permanently-idle pool that does not exist."""
    app, ctx = _edge(tmp_path)
    with ctx, TestClient(app, raise_server_exceptions=False) as client:
        body = client.get("/api/daemon/health", headers=_IDENT).json()
        assert body["pool"] is None


def test_daemon_health_survives_a_broken_pool_reporter(tmp_path):
    """Telemetry must never take down the route an operator reads during an outage."""
    app, ctx = _edge(tmp_path)
    with ctx, TestClient(app, raise_server_exceptions=False) as client:
        def _boom():
            raise RuntimeError("httpcore moved the pool")

        app.state.sessions = types.SimpleNamespace(daemon_pool_stats=_boom)
        resp = client.get("/api/daemon/health", headers=_IDENT)
        assert resp.status_code == 200, resp.text
        assert resp.json()["pool"] is None
