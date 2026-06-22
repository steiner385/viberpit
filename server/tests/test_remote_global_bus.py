import asyncio
from unittest import mock
import pytest
from server.ipc.client import DaemonClient
from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.remote import RemoteOrchestrator
from server.tests.ipc_harness import serve_daemon_uds


@pytest.mark.asyncio
async def test_global_events_reach_remote_queue():
    app = create_daemon_app()
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda n: None)
    register_orchestrator_rpc(app, orch)
    async with serve_daemon_uds(app) as sock:
        ro = RemoteOrchestrator(DaemonClient(sock))
        try:
            q = await ro.subscribe_events()
            await asyncio.sleep(0.1)  # let the feeder subscribe to "_global"
            orch._publish_event({"type": "rotated", "sessionId": "s1"})
            ev = await asyncio.wait_for(q.get(), 2)
            assert ev == {"type": "rotated", "sessionId": "s1"}
            await ro.unsubscribe_events(q)
        finally:
            await ro.aclose()


@pytest.mark.asyncio
async def test_global_events_fan_out_to_multiple_subscribers():
    """Two concurrent subscribe_events() callers each receive every published event.

    After unsubscribe_events(q1), subsequent events reach q2 but not q1.
    """
    app = create_daemon_app()
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda n: None)
    register_orchestrator_rpc(app, orch)
    async with serve_daemon_uds(app) as sock:
        ro = RemoteOrchestrator(DaemonClient(sock))
        try:
            q1 = await ro.subscribe_events()
            q2 = await ro.subscribe_events()
            await asyncio.sleep(0.1)  # let the pump subscribe to "_global"

            # Both subscribers receive the first event.
            orch._publish_event({"type": "rotated", "sessionId": "s1"})
            ev1 = await asyncio.wait_for(q1.get(), 2)
            ev2 = await asyncio.wait_for(q2.get(), 2)
            assert ev1 == {"type": "rotated", "sessionId": "s1"}
            assert ev2 == {"type": "rotated", "sessionId": "s1"}

            # After unsubscribing q1, only q2 receives subsequent events.
            await ro.unsubscribe_events(q1)
            orch._publish_event({"type": "sessionEnded", "sessionId": "s1"})
            ev2b = await asyncio.wait_for(q2.get(), 2)
            assert ev2b == {"type": "sessionEnded", "sessionId": "s1"}

            # q1 must NOT have received the second event.
            await asyncio.sleep(0.1)
            assert q1.empty(), "q1 should be empty after unsubscribe"

            await ro.unsubscribe_events(q2)
        finally:
            await ro.aclose()
