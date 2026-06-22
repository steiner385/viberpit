from datetime import datetime, timezone
from unittest import mock
import pytest
from ccswitch.oauth import Usage
from server.ipc.client import DaemonClient
from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.remote import RemoteOrchestrator
from server.tests.ipc_harness import serve_daemon_uds


def _build():
    app = create_daemon_app()
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda n: None)
    register_orchestrator_rpc(app, orch)
    return app, orch


@pytest.mark.asyncio
async def test_usage_round_trips_a_usage_object():
    app, orch = _build()
    u = Usage(50.0, datetime(2026, 6, 22, tzinfo=timezone.utc), 10.0, None, False)
    orch.poller.get = mock.AsyncMock(return_value=u)
    async with serve_daemon_uds(app) as sock:
        ro = RemoteOrchestrator(DaemonClient(sock))
        try:
            got = await ro.usage("acct")
        finally:
            await ro.aclose()
    assert got == u


@pytest.mark.asyncio
async def test_pending_permissions_and_active_count():
    app, orch = _build()
    orch._registry.pending = mock.MagicMock(return_value=[{"permId": "p1", "tool": "Bash"}])
    orch.active_session_count = mock.MagicMock(return_value=3)
    async with serve_daemon_uds(app) as sock:
        ro = RemoteOrchestrator(DaemonClient(sock))
        try:
            assert await ro.pending_permissions("s1") == [{"permId": "p1", "tool": "Bash"}]
            assert await ro.active_session_count() == 3
        finally:
            await ro.aclose()
