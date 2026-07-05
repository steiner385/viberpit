"""Regression: the per-session publish pump must follow a driver swap.

Live transcript went permanently stale (until reattach forced a disk replay) when a
session's driver was torn down + rebuilt after the pump started — e.g. a watchdog/
token recompose (`_recompose_same_account`) right after a daemon restart. The pump
was gated by an `app.state.pumped` SET that was added-to but never cleared, so once
the original driver's `events()` ended (or a new driver object replaced it), no
later `ensure_driver` (reattach) could ever restart the pump. Events published into
a dead/abandoned generator; the session's published-event count froze.
"""
import asyncio
import pytest

from server.ipc.client import DaemonClient
from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.tests.ipc_harness import serve_daemon_uds


class _Evt:
    def __init__(self, i): self.i = i
    def to_wire(self): return {"type": "textDelta", "i": self.i}


class _EndingDriver:
    """Emits its values then its stream ENDS — mirrors a driver torn down by a recompose."""
    def __init__(self, *vals): self._vals = vals
    def current_status(self): return "idle"
    def queued(self): return []

    async def events(self):
        for v in self._vals:
            yield _Evt(v)
        # generator returns → pump task ends (as aclose()/_STREAM_DONE would do)


class _QueueDriver:
    """Stays alive, emitting whatever the test feeds into its queue."""
    def __init__(self, q: asyncio.Queue): self._q = q
    def current_status(self): return "idle"
    def queued(self): return []

    async def events(self):
        while True:
            yield await self._q.get()


class _Orch:
    """Minimal orchestrator: ensure_driver hands back the queued drivers in order."""
    def __init__(self, *drivers):
        self._drivers = list(drivers)
        self._calls = 0

    def subscribe_events(self) -> asyncio.Queue:
        return asyncio.Queue()  # global pump source; never fed in these tests

    async def ensure_driver(self, session_id, identity=None):
        d = self._drivers[min(self._calls, len(self._drivers) - 1)]
        self._calls += 1
        return d


@pytest.mark.asyncio
async def test_pump_restarts_for_replacement_driver_after_recompose():
    app = create_daemon_app()
    d1 = _EndingDriver(0)          # emits 0, then its stream ends (recompose tears it down)
    d2 = _EndingDriver(99)         # the replacement driver that now drives the session
    register_orchestrator_rpc(app, _Orch(d1, d2))
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            stream = client.subscribe("s1")
            await client.events_ready("s1")
            await client.call("ensure_driver", session_id="s1")          # pump d1
            e1 = await asyncio.wait_for(stream.__anext__(), 2)
            # d1's stream has ended; a recompose swapped in d2. A reattach re-ensures
            # the driver and MUST restart the pump on the new driver.
            await client.call("ensure_driver", session_id="s1")          # must pump d2
            e2 = await asyncio.wait_for(stream.__anext__(), 2)
        finally:
            await client.aclose()
    assert e1["i"] == 0
    assert e2["i"] == 99


@pytest.mark.asyncio
async def test_driver_replaced_hook_restarts_pump_without_rpc():
    """A recovery swap (orchestrator._reassign) replaces the driver with NO RPC, so no
    ensure_driver runs to restart the pump. The orchestrator fires on_driver_replaced;
    the daemon must restart the pump on the NEW driver so an ALREADY-attached viewer
    keeps receiving live events — otherwise its transcript freezes until a reattach."""
    app = create_daemon_app()
    q1: asyncio.Queue = asyncio.Queue()
    q2: asyncio.Queue = asyncio.Queue()
    d1 = _QueueDriver(q1)
    d2 = _QueueDriver(q2)          # the swapped-in driver after a rotation / 401 recompose
    orch = _Orch(d1)
    register_orchestrator_rpc(app, orch)
    assert orch.on_driver_replaced is not None      # hook registered by the RPC layer
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            stream = client.subscribe("s1")
            await client.events_ready("s1")
            await client.call("ensure_driver", session_id="s1")      # pump d1
            await q1.put(_Evt(1))
            e1 = await asyncio.wait_for(stream.__anext__(), 2)
            assert e1["i"] == 1
            # Recovery swaps d1 → d2 with NO ensure_driver RPC. Fire the hook the way
            # orchestrator._install_driver does; the pump must follow to d2.
            orch.on_driver_replaced("s1", d2)
            await q2.put(_Evt(2))
            e2 = await asyncio.wait_for(stream.__anext__(), 2)
            assert e2["i"] == 2
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_driver_replaced_hook_same_driver_pumps_once():
    """Idempotency: firing the hook with the SAME driver already being pumped must not
    start a 2nd pump (a redundant swap notification would double-publish every event)."""
    app = create_daemon_app()
    q: asyncio.Queue = asyncio.Queue()
    d = _QueueDriver(q)
    orch = _Orch(d)
    register_orchestrator_rpc(app, orch)
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            stream = client.subscribe("s1")
            await client.events_ready("s1")
            await client.call("ensure_driver", session_id="s1")      # pump d
            orch.on_driver_replaced("s1", d)                          # same driver → no 2nd pump
            await q.put(_Evt(5))
            e = await asyncio.wait_for(stream.__anext__(), 2)
            assert e["i"] == 5
            with pytest.raises(asyncio.TimeoutError):                  # no duplicate copy
                await asyncio.wait_for(stream.__anext__(), 0.3)
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_ensure_driver_snapshot_carries_turn_started_at():
    """The ensure_driver RPC snapshot must expose turn_started_at alongside
    turn_active: the edge's RemoteDriver hands it to replay_window so the
    incomplete-turn drop only amputates the turn that actually IS in progress
    (resume-lag bug: the disk's last user entry can be a PREVIOUS turn's)."""
    app = create_daemon_app()
    fed: asyncio.Queue = asyncio.Queue()
    register_orchestrator_rpc(app, _Orch(_QueueDriver(fed)))
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            # No open turn yet → None (matches turn_active False).
            r0 = await client.call("ensure_driver", session_id="s1")
            assert r0["turn_active"] is False
            assert r0["turn_started_at"] is None
            # Open a turn on the daemon side; the snapshot must carry its start ts.
            await app.state.publish("s1", {"type": "user", "text": "hi"})
            r1 = await client.call("ensure_driver", session_id="s1")
            assert r1["turn_active"] is True
            assert isinstance(r1["turn_started_at"], float)
            # Turn settles → back to None.
            await app.state.publish("s1", {"type": "turnEnd", "turnId": "t"})
            r2 = await client.call("ensure_driver", session_id="s1")
            assert r2["turn_active"] is False
            assert r2["turn_started_at"] is None
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_repeated_ensure_driver_same_driver_pumps_once():
    """Idempotency guard: re-ensuring the SAME live driver must not start a 2nd pump
    (which would double-publish every event)."""
    app = create_daemon_app()
    fed: asyncio.Queue = asyncio.Queue()
    d = _QueueDriver(fed)
    register_orchestrator_rpc(app, _Orch(d, d))
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            stream = client.subscribe("s1")
            await client.events_ready("s1")
            await client.call("ensure_driver", session_id="s1")
            await client.call("ensure_driver", session_id="s1")          # same driver → no 2nd pump
            await fed.put(_Evt(7))
            e = await asyncio.wait_for(stream.__anext__(), 2)
            assert e["i"] == 7
            with pytest.raises(asyncio.TimeoutError):                      # no duplicate copy
                await asyncio.wait_for(stream.__anext__(), 0.3)
        finally:
            await client.aclose()
