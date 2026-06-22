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
