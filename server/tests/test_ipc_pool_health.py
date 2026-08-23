"""A wedged RPC pool must be VISIBLE — no `ss -tn state close-wait` required (issue #734).

Both edge outages had the same diagnostic shape: every daemon-backed route 503'd after
exactly the pool timeout, the journal said only that individual RPCs failed, and the
actual cause — all `max_connections` slots pinned ACTIVE by stranded connections — was
found by hand with a socket dump. The system knew; it just never said.

These tests pin the two things that fix that:
  1. ``DaemonClient.pool_stats()`` reports the occupancy that distinguishes "the daemon is
     down" from "we cannot reach a healthy daemon because our own pool is full".
  2. A pool timeout is counted and logged at WARNING with that occupancy attached, so the
     wedge is legible in the journal as it happens.
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

    register_rpc(app, "hang", _hang)
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
    expired, or surplus-IDLE, so ``inUse == maxConnections`` is not a busy moment; it is a
    pool that cannot recover on its own.
    """
    client = _client(daemon, mtls_certs, max_connections=2, monkeypatch=monkeypatch)
    try:
        inflight = await _saturate(client, daemon, 2)

        stats = client.pool_stats()
        assert stats["inUse"] == 2 == stats["maxConnections"]
        assert stats["idle"] == 0, "a wedged pool must not report reclaimable connections"
        assert stats["saturated"] is True

        # A caller arriving now is queued behind a pool that will not free a slot.
        queued = asyncio.create_task(client.call("echo", stream="s", text="wedged"))
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
                await client.call("echo", stream="s", text="starved")

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
async def test_a_completed_rpc_clears_the_consecutive_count(daemon, mtls_certs,
                                                            monkeypatch):
    """``consecutivePoolTimeouts`` is the wedge indicator, so it must mean "right now".
    One starved request during a fan-out burst is not a wedge; a count that never resets
    would read as one forever."""
    client = _client(daemon, mtls_certs, max_connections=1, monkeypatch=monkeypatch)
    try:
        inflight = await _saturate(client, daemon, 1)
        with pytest.raises(DaemonTimeout):
            await client.call("echo", stream="s", text="starved")
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
                    await client.call("echo", stream="s", text="starved")

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
