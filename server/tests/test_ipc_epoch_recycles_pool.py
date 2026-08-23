"""A daemon restart must make the edge's HTTP pool heal, not wedge (issue #734).

When the session daemon restarts, every connection the edge holds in its httpx pool
points at a process that no longer exists. Nothing discarded them: 59 were observed
stranded while a *fresh* connection to the same daemon answered in 0.2s, every
RPC-backed route 503'd after exactly the pool timeout, and only
`systemctl restart ccswitch.service` cleared it — so deploying a fix broke the app
until a human intervened (spike:
``docs/superpowers/spikes/2026-08-23-edge-socket-leak.md``).

The connections that matter are pinned **ACTIVE**, not idle. httpcore only evicts a
connection that is closed, expired, or surplus-*idle*, so an ACTIVE one holds its socket
and one of ``max_connections`` slots for the life of the process — no sweep can reach it.
These tests build that exact pool state and assert the fingerprint before asserting the
fix, so an implementation that only closes idle connections cannot pass them.

An ACTIVE connection is produced here by a genuinely in-flight RPC against a daemon that
does not answer. That is the same pool state a stranded connection has — the pool cannot
tell them apart, which is precisely why it can never reap one — and it also exercises the
hard part of the design: the recycle *does* error requests that are still in flight. Most
were opened before the restart and were already doomed; a few, opened to the new daemon
before the reader noticed the restart, are healthy collateral. Either way they must fail
TYPED and fast, which is what these tests pin.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
import time
import types

import httpx
import pytest
import pytest_asyncio
import uvicorn

from server.ipc import client as ipc_client
from server.ipc.client import DaemonClient, DaemonTimeout, DaemonUnavailable
from server.ipc.daemon import create_daemon_app, register_rpc
from server.ipc.tls import ClientTLSFiles
from server.tests.ipc_harness import serve_daemon_tls

# Generous: nothing here should take this long, and a slow CI box must not turn a
# correctness assertion into a flake.
_SETTLE = 5.0


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def daemon(mtls_certs):
    """A real mTLS daemon with a ``hang`` RPC that never answers until released.

    ``hang`` is what pins a connection ACTIVE: the TLS handshake completed, the request
    is on the wire, and no response is coming. The daemon itself stays healthy
    throughout — that is the whole point, and it is what the field failure looked like.
    """
    app = create_daemon_app()
    state = types.SimpleNamespace(release=asyncio.Event(), started=0)

    async def _hang(params: dict) -> dict:
        state.started += 1
        # Bounded so a failed test can never wedge uvicorn's graceful shutdown.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(state.release.wait(), 20)
        return {"released": True}

    register_rpc(app, "hang", _hang)
    async with serve_daemon_tls(app, mtls_certs) as endpoint:
        state.endpoint = endpoint
        try:
            yield state
        finally:
            state.release.set()
            await asyncio.sleep(0.05)     # let the handlers unwind before shutdown


def _client(daemon, certs, *, max_connections: int, keepalive_expiry: float = 5.0,
            monkeypatch=None) -> DaemonClient:
    """A DaemonClient with a smaller pool, built through the module's own limits.

    ``_LIMITS`` is patched rather than passed, so the client is still constructed by the
    single code path under test — nothing here is a parallel construction.
    """
    monkeypatch.setattr(ipc_client, "_LIMITS", httpx.Limits(
        max_connections=max_connections, max_keepalive_connections=max_connections,
        keepalive_expiry=keepalive_expiry))
    tls = ClientTLSFiles(cert=certs.client_cert, key=certs.client_key, ca=certs.ca)
    return DaemonClient(daemon.endpoint, client_tls=tls)


def _pool(client: DaemonClient):
    """The httpcore pool behind the client — where the wedge happens."""
    return client._http._transport._pool


async def _pin_active(client: DaemonClient, daemon, n: int):
    """Occupy *n* pool slots with connections pinned ACTIVE.

    Returns ``(connections, in-flight tasks)``.
    """
    tasks = [asyncio.create_task(client.call("hang")) for _ in range(n)]
    deadline = time.monotonic() + _SETTLE
    while time.monotonic() < deadline:
        if daemon.started >= n and len(_pool(client).connections) >= n:
            break
        await asyncio.sleep(0.02)
    assert daemon.started == n, f"only {daemon.started}/{n} RPCs reached the daemon"
    conns = _pool(client).connections
    assert len(conns) == n, f"expected {n} pooled connections, got {len(conns)}"
    for t in tasks:
        assert not t.done(), "the daemon answered — the connection is not pinned"
    return conns, tasks


def _assert_pinned_active(conns) -> None:
    """The property that makes these connections unreachable from inside the pool.

    If this fingerprint ever stops holding, httpcore changed its eviction rules and the
    recycle may no longer be needed — investigate, don't delete.
    """
    assert conns, "test is vacuous — nothing is pooled"
    for c in conns:
        assert not c.is_idle(), f"{c!r} is idle — a close-idle-connections sweep would reclaim it"
        assert not c.is_closed(), f"{c!r} is already closed — nothing to reclaim"
        assert not c.has_expired(), f"{c!r} has expired — the pool would evict it unaided"
        assert not c.is_available(), f"{c!r} is available — it is not holding a slot"


async def _serve_uds(app, sock: str):
    server = uvicorn.Server(uvicorn.Config(app, uds=sock, log_level="error",
                                           lifespan="off"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    return server, task


async def _stop_uds(server, task) -> None:
    server.should_exit = True
    await task


# --------------------------------------------------------------------------- #
# 1. The fix — an epoch change reclaims ACTIVE connections
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_epoch_change_reclaims_active_connections(daemon, mtls_certs, monkeypatch):
    """The load-bearing case, driven by the REAL ``/info`` epoch comparison."""
    client = _client(daemon, mtls_certs, max_connections=4, monkeypatch=monkeypatch)
    try:
        pinned, inflight = await _pin_active(client, daemon, 3)
        _assert_pinned_active(pinned)

        # The daemon this edge last talked to is gone; the one answering now is a new
        # process, so /info reports an epoch this client has never seen.
        client._epoch = "epoch-of-the-dead-process"
        await client._reconcile_epoch()
        assert client._epoch != "epoch-of-the-dead-process", "/info never reached the daemon"

        pooled = _pool(client).connections
        for c in pinned:
            assert c not in pooled, f"{c!r} kept its pool slot across the restart"
            assert c.is_closed(), f"{c!r} lost its slot but kept its socket"

        # The requests riding those connections fail fast and TYPED, rather than hanging
        # until their read deadline. Typed is the load-bearing half: a caller degrades on
        # DaemonUnavailable, so even a healthy request killed as collateral cannot 500.
        for result in await asyncio.gather(*inflight, return_exceptions=True):
            assert isinstance(result, DaemonUnavailable), result
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_a_wedged_pool_recovers_after_the_epoch_changes(daemon, mtls_certs,
                                                              monkeypatch):
    """The wedge itself, healed on the SAME client.

    The spike pinned a pool at ``max_connections`` and showed every follow-up timing out
    against a healthy endpoint, recoverable only by tearing the client down (= restarting
    the process). Same shape here: saturate, confirm the wedge, change the epoch, confirm
    RPCs flow again.

    ``info`` is stubbed here so this test isolates the *recycle* half of the heal. The
    unstubbed path — a saturated pool that still reaches ``/info`` over its reserved
    channel — is ``test_a_saturated_pool_still_sees_the_restart_signal`` below.
    """
    client = _client(daemon, mtls_certs, max_connections=2, monkeypatch=monkeypatch)
    try:
        inflight = [asyncio.create_task(client.call("hang")) for _ in range(2)]
        deadline = time.monotonic() + _SETTLE
        while daemon.started < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        _assert_pinned_active(_pool(client).connections)

        # The wedge: the daemon is healthy, but no RPC can get a connection.
        seen = daemon.started
        t0 = time.monotonic()
        with pytest.raises(DaemonTimeout):
            await client.call("echo", stream="s", text="wedged")
        assert daemon.started == seen, "the request reached the daemon — pool not saturated"
        assert time.monotonic() - t0 < _SETTLE, "the pool wait must stay bounded (task 2)"

        async def _restarted():
            return {"epoch": "E-new"}

        client._epoch, client.info = "E-old", _restarted   # type: ignore[assignment]
        await client._reconcile_epoch()

        # In-flight RPCs are errored, not left hanging. Whether an individual one was
        # doomed anyway (opened pre-restart) or is healthy collateral (opened to the new
        # daemon inside the reconnect window), it must fail as a typed DaemonUnavailable —
        # the failure every call site degrades on via gather(return_exceptions=True).
        done, pending = await asyncio.wait(inflight, timeout=_SETTLE)
        assert not pending, "an in-flight RPC survived the recycle and is still hanging"
        for t in done:
            with pytest.raises(DaemonUnavailable):
                t.result()

        t0 = time.monotonic()
        assert await client.call("echo", stream="s", text="healed") == {"ok": True}
        assert time.monotonic() - t0 < _SETTLE, "the healed RPC queued for a slot"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_a_saturated_pool_still_sees_the_restart_signal(daemon, mtls_certs,
                                                              monkeypatch):
    """The self-heal must survive the state it exists to fix: a FULL pool.

    This is the gap the epoch recycle could not close on its own. ``/info`` used to ride
    the same pool as every RPC, so once all ``max_connections`` slots were pinned ACTIVE
    it raised ``PoolTimeout`` after the 2s pool leg, ``_reconcile_epoch`` swallowed it in
    its ``except Exception: return``, and the recycle never fired. The WS reader calls
    ``_reconcile_epoch`` once per reconnect, so a pool already saturated at restart-time
    stayed wedged for the life of the process — exactly the state both production outages
    reached, each cleared only by ``systemctl restart ccswitch.service``.

    Nothing is stubbed: the restart signal comes from the REAL ``/info``, over a pool with
    zero free slots. That is the whole assertion.
    """
    client = _client(daemon, mtls_certs, max_connections=2, monkeypatch=monkeypatch)
    try:
        pinned, inflight = await _pin_active(client, daemon, 2)
        _assert_pinned_active(pinned)

        # The wedge fingerprint: the daemon is healthy, but the RPC pool has nothing left
        # to give. Any caller sharing this pool — /info included, before the fix — waits
        # the pool leg and fails.
        seen = daemon.started
        with pytest.raises(DaemonTimeout):
            await client.call("echo", stream="s", text="wedged")
        assert daemon.started == seen, "the request reached the daemon — pool not saturated"

        # The daemon answering now reports an epoch this client has never seen, i.e. the
        # process it was talking to is gone. The signal must get through anyway.
        client._epoch = "epoch-of-the-dead-process"
        await client._reconcile_epoch()
        assert client._epoch != "epoch-of-the-dead-process", (
            "/info could not reach the daemon through a saturated pool — the restart "
            "signal is still starved by the RPC pool")

        # ...and having got through, it healed the pool.
        pooled = _pool(client).connections
        for c in pinned:
            assert c not in pooled, f"{c!r} kept its pool slot across the restart"
            assert c.is_closed(), f"{c!r} lost its slot but kept its socket"
        for result in await asyncio.gather(*inflight, return_exceptions=True):
            assert isinstance(result, DaemonUnavailable), result

        # The edge is serving RPCs again with no process restart.
        t0 = time.monotonic()
        assert await client.call("echo", stream="s", text="healed") == {"ok": True}
        assert time.monotonic() - t0 < _SETTLE, "the healed RPC queued for a slot"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_the_health_channel_never_competes_with_rpcs_for_a_slot(daemon, mtls_certs,
                                                                      monkeypatch):
    """``/info`` must not consume an RPC pool slot, in either direction.

    The reserved channel is only reserved if it is genuinely separate: a ``/info`` call
    must leave the RPC pool's occupancy untouched, and an RPC must not be able to occupy
    the health channel.
    """
    client = _client(daemon, mtls_certs, max_connections=2, monkeypatch=monkeypatch)
    try:
        pinned, inflight = await _pin_active(client, daemon, 2)

        assert await client.info(), "/info returned nothing through a saturated pool"

        assert _pool(client).connections == pinned, (
            "/info disturbed the RPC pool — it is not on a channel of its own")
        for t in inflight:
            assert not t.done(), "an in-flight RPC was collateral of the /info call"
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- #
# 2. An UNCHANGED epoch must not recycle
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_unchanged_epoch_does_not_recycle(daemon, mtls_certs, monkeypatch):
    """36 reconnects in 40 minutes were observed during relay instability. Churning the
    pool on every one of them would be its own performance problem.

    ``keepalive_expiry`` is raised so that an expiry-driven eviction can never be
    mistaken for a recycle.
    """
    client = _client(daemon, mtls_certs, max_connections=4, keepalive_expiry=60.0,
                     monkeypatch=monkeypatch)
    try:
        await client._reconcile_epoch()              # first sight records the epoch
        assert client._epoch is not None
        await client.call("echo", stream="s", text="warm")
        before = _pool(client).connections
        assert before, "nothing pooled to preserve — test is vacuous"

        await client._reconcile_epoch()              # same daemon, same epoch

        after = _pool(client).connections
        assert all(c in after for c in before), "a routine reconnect discarded connections"
        assert not any(c.is_closed() for c in before)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_first_sight_of_an_epoch_does_not_recycle(daemon, mtls_certs, monkeypatch):
    """Cold start: ``_epoch`` is None, so there is no *change* to react to."""
    client = _client(daemon, mtls_certs, max_connections=4, keepalive_expiry=60.0,
                     monkeypatch=monkeypatch)
    try:
        assert client._epoch is None
        await client.call("echo", stream="s", text="warm")
        before = _pool(client).connections
        assert before

        await client._reconcile_epoch()

        assert all(c in _pool(client).connections for c in before)
        assert not any(c.is_closed() for c in before)
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- #
# 3. The recycle must not cost the reconnect path anything it needs
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_recycle_keeps_the_state_the_resubscribe_path_needs(daemon, mtls_certs,
                                                                  monkeypatch):
    """Recycling is an HTTP-pool operation. The WS reader resubscribes every ``_desired``
    stream immediately after ``_reconcile_epoch`` returns, so that set (and its readiness
    events) must survive; only the seq watermarks reset, because the restarted daemon's
    seq space is new."""
    client = _client(daemon, mtls_certs, max_connections=4, monkeypatch=monkeypatch)
    try:
        client._desired.update({"s1", "s2"})
        client._ready["s1"] = asyncio.Event()
        client._last_seq["s1"] = 42
        client._baseline["s1"] = 42
        client._epoch = "E-old"

        await client._reconcile_epoch()

        assert client._desired == {"s1", "s2"}, "the resubscribe set was lost"
        assert "s1" in client._ready
        assert "s1" not in client._last_seq, "a stale watermark survived the restart"
        assert client._baseline["s1"] is None
        assert client._since_for("s1") is None, "next subscribe must ask for a full replay"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_reader_resubscribes_after_a_real_daemon_restart():
    """End to end over the real reconnect path, with the recycle in the middle of it:
    a brand-new daemon process on the same socket, and events still reach the subscriber.
    """
    tmp = tempfile.mkdtemp(prefix="ccsd-recycle-")
    sock = os.path.join(tmp, "d.sock")
    received: list[dict] = []
    client = DaemonClient(sock)
    drain = None
    srv_b = task_b = None

    app_a = create_daemon_app()
    srv_a, task_a = await _serve_uds(app_a, sock)
    try:
        stream = client.subscribe("s1")
        await client.events_ready("s1")

        async def _drain():
            async for ev in stream:
                received.append(ev)
        drain = asyncio.create_task(_drain())

        await client.call("echo", stream="s1", text="before")
        await asyncio.sleep(0.2)
        assert any(e.get("text") == "before" for e in received)
        epoch_a = client.epoch
        assert epoch_a is not None
        stale = _pool(client).connections            # opened to daemon A
        assert stale

        # ---- restart: a NEW process on the same socket ----
        await _stop_uds(srv_a, task_a)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(sock)
        srv_b, task_b = await _serve_uds(create_daemon_app(), sock)

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and client.epoch == epoch_a:
            await asyncio.sleep(0.05)                # reader reconnects + reconciles
        assert client.epoch != epoch_a, "the reader never noticed the restart"

        assert not any(c in _pool(client).connections for c in stale), (
            "connections to the dead daemon process are still pooled")
        await client.call("echo", stream="s1", text="after")
        await asyncio.sleep(0.3)
        assert any(e.get("text") == "after" for e in received), (
            "events from the restarted daemon never arrived — resubscribe path broken")
    finally:
        if drain is not None:
            drain.cancel()
        if srv_b is not None:
            with contextlib.suppress(Exception):
                await _stop_uds(srv_b, task_b)
        await client.aclose()
        with contextlib.suppress(OSError):
            os.unlink(sock)
        with contextlib.suppress(OSError):
            os.rmdir(tmp)


# --------------------------------------------------------------------------- #
# 4. Failure modes — never leave the client worse off than the wedge
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_failing_recycle_leaves_the_client_usable(daemon, mtls_certs, monkeypatch):
    """A recycle that blows up must not brick the client — that would be worse than the
    wedge it fixes. (Closing the ``AsyncClient`` itself would do exactly that: httpx
    flips it to CLOSED and every later request raises ``RuntimeError``.)"""
    client = _client(daemon, mtls_certs, max_connections=4, monkeypatch=monkeypatch)
    try:
        await client.call("echo", stream="s", text="warm")
        attempts = 0
        real_aclose = client._transport.aclose

        async def _explode():
            nonlocal attempts
            attempts += 1
            if attempts == 1:                        # only the recycle blows up
                raise RuntimeError("pool teardown blew up")
            await real_aclose()

        monkeypatch.setattr(client._transport, "aclose", _explode)
        client._epoch = "E-old"

        await client._reconcile_epoch()              # must not raise

        assert attempts == 1, "the recycle was never attempted"
        assert client._epoch != "E-old", "epoch bookkeeping was skipped after a failed recycle"
        assert await client.call("echo", stream="s", text="after") == {"ok": True}
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_unreachable_info_neither_recycles_nor_raises(daemon, mtls_certs,
                                                            monkeypatch):
    """Mid-restart, ``/info`` is momentarily unreachable. That is not evidence of a new
    epoch, so it must neither recycle nor propagate — the reader retries next loop."""
    client = _client(daemon, mtls_certs, max_connections=4, keepalive_expiry=60.0,
                     monkeypatch=monkeypatch)
    try:
        await client.call("echo", stream="s", text="warm")
        before = _pool(client).connections
        assert before

        async def _down():
            raise DaemonUnavailable("connection refused")

        client.info = _down                          # type: ignore[assignment]
        client._epoch = "E-old"

        await client._reconcile_epoch()              # must not raise

        assert client._epoch == "E-old", "an unreachable /info must not touch the epoch"
        assert all(c in _pool(client).connections for c in before), (
            "an unreachable daemon recycled the pool")
        assert not any(c.is_closed() for c in before)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_info_without_an_epoch_does_not_recycle(daemon, mtls_certs, monkeypatch):
    """An ``/info`` payload with no epoch carries no restart signal."""
    client = _client(daemon, mtls_certs, max_connections=4, keepalive_expiry=60.0,
                     monkeypatch=monkeypatch)
    try:
        await client.call("echo", stream="s", text="warm")
        before = _pool(client).connections

        async def _no_epoch():
            return {"sha": "abc123"}

        client.info = _no_epoch                      # type: ignore[assignment]
        client._epoch = "E-old"

        await client._reconcile_epoch()

        assert client._epoch == "E-old"
        assert all(c in _pool(client).connections for c in before)
    finally:
        await client.aclose()
