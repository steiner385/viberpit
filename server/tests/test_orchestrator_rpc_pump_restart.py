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
import contextlib

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


# ---------------------------------------------------------------------------
# Supervision of the fire-and-forget pump tasks (audit W2.6)
# ---------------------------------------------------------------------------
# A pump/global-pump created bare (no done-callback) swallows an unexpected
# exception: the task dies, its work stops, and NOTHING logs, restarts, or
# signals. `supervise` observes the failure, logs loudly, and applies a bounded
# one-retry-then-give-up recovery so a wedged driver can't hot-loop.


class _RaisingDriver:
    """events() yields *boom_after* values then RAISES — a pump crash. Counts starts
    so a test can assert how many times the pump was (re)started."""
    def __init__(self, *, boom_after: int = 0):
        self.boom_after = boom_after
        self.starts = 0
    def current_status(self): return "idle"
    def queued(self): return []

    async def events(self):
        self.starts += 1
        for i in range(self.boom_after):
            yield _Evt(i)
        raise RuntimeError("pump boom")


@pytest.mark.asyncio
async def test_pump_restarts_once_then_gives_up_on_repeated_failure(caplog):
    """A _pump whose driver.events() raises must be restarted exactly ONCE; the
    second consecutive failure logs and gives up — no hot loop."""
    app = create_daemon_app()
    d = _RaisingDriver()                       # raises immediately, every start
    register_orchestrator_rpc(app, _Orch(d, d))
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            with caplog.at_level("ERROR"):
                await client.call("ensure_driver", session_id="s1")   # start pump → crash
                await asyncio.sleep(0.3)                              # let the retry run + crash
        finally:
            await client.aclose()
    assert d.starts == 2, "expected initial start + exactly one restart"
    driver, count = app.state.pump_failures["s1"]
    assert driver is d and count == 2          # 2nd failure recorded, then gave up
    assert any("giving up" in r.getMessage() for r in caplog.records), \
        "give-up must be logged loudly"


@pytest.mark.asyncio
async def test_pump_auto_restart_delivers_after_transient_failure():
    """The auto-retry (no reattach) actually recovers: a driver that raises on its
    FIRST events() call but streams on the restart keeps the live transcript flowing."""
    app = create_daemon_app()
    q: asyncio.Queue = asyncio.Queue()

    class _FlakyDriver:
        def __init__(self): self.starts = 0
        def current_status(self): return "idle"
        def queued(self): return []
        async def events(self):
            self.starts += 1
            if self.starts == 1:
                raise RuntimeError("transient pump boom")
            while True:
                yield await q.get()

    d = _FlakyDriver()
    register_orchestrator_rpc(app, _Orch(d))
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            stream = client.subscribe("s1")
            await client.events_ready("s1")
            await client.call("ensure_driver", session_id="s1")   # pump → crash → auto-restart (no RPC)
            await asyncio.sleep(0.2)
            await q.put(_Evt(42))
            e = await asyncio.wait_for(stream.__anext__(), 2)
            assert e["i"] == 42
            assert d.starts == 2      # crashed once, auto-restarted once, then healthy
        finally:
            await client.aclose()


class _RaisingQueue:
    """A subscribe_events() queue whose get() raises — forces the global pump to crash."""
    async def get(self):
        raise RuntimeError("global boom")


class _GlobalOrch(_Orch):
    """Global-pump source: first subscribe hands back a crashing queue, later
    subscribes hand back a blocking (healthy) queue. Counts subscribes so a test
    can assert the pump restarted."""
    def __init__(self, *drivers):
        super().__init__(*drivers)
        self.subscribe_calls = 0

    def subscribe_events(self):
        self.subscribe_calls += 1
        if self.subscribe_calls == 1:
            return _RaisingQueue()
        return asyncio.Queue()        # blocks forever → restarted pump survives


@pytest.mark.asyncio
async def test_global_pump_restarts_once_on_failure():
    """The _global_pump crashing (its event source raised) must restart once and
    recover — otherwise ALL lifecycle/status frames to every edge stop forever."""
    orch = _GlobalOrch()
    app = create_daemon_app()
    register_orchestrator_rpc(app, orch)      # starts global pump #1 on this loop
    await asyncio.sleep(0.3)                   # #1 crashes → restart → #2 subscribes + blocks
    assert orch.subscribe_calls == 2, "global pump must restart exactly once"
    assert app.state.global_pump_failures == 1
    assert not app.state.global_pump.done(), "the restarted global pump must be alive"
    app.state.global_pump.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await app.state.global_pump
