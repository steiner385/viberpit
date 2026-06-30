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
