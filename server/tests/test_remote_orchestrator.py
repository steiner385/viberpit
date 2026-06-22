import asyncio
from unittest import mock
import pytest
from server.ipc.client import DaemonClient, DaemonUnavailable
from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.remote import RemoteOrchestrator
from server.sessions.types import SessionRecord
from server.tests.ipc_harness import serve_daemon_uds


def _rec(sid="s1"):
    return SessionRecord(id=sid, name="S1", account="acct", claude_session_id="cs",
                         cols=80, rows=24, cwd="/tmp")


class _FakeEvent:
    def to_wire(self): return {"type": "textDelta", "turnId": "t1", "text": "hi"}


class _FakeDriver:
    async def events(self):
        yield _FakeEvent()


def _build_app():
    app = create_daemon_app()
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda name: None)
    register_orchestrator_rpc(app, orch)
    return app, orch


@pytest.mark.asyncio
async def test_list_get_create_send_over_uds():
    app, orch = _build_app()
    orch._records["s1"] = _rec("s1")
    with mock.patch.object(orch, "create_session",
                           new=mock.AsyncMock(return_value=_rec("new1"))), \
         mock.patch.object(orch, "send_prompt", new=mock.AsyncMock()) as sp:
        async with serve_daemon_uds(app) as sock:
            ro = RemoteOrchestrator(DaemonClient(sock))
            try:
                listed = await ro.list_sessions()
                got = await ro.get_session("s1")
                missing = await ro.get_session("nope")
                created = await ro.create_session(name="S", account="acct")
                await ro.send_prompt("s1", "hello")
            finally:
                await ro.aclose()
    assert [r.id for r in listed] == ["s1"]
    assert got.id == "s1" and missing is None
    assert created.id == "new1"
    sp.assert_awaited_once_with("s1", "hello", None)


@pytest.mark.asyncio
async def test_ensure_driver_and_stream_over_uds():
    app, orch = _build_app()
    with mock.patch.object(orch, "ensure_driver",
                           new=mock.AsyncMock(return_value=_FakeDriver())):
        async with serve_daemon_uds(app) as sock:
            ro = RemoteOrchestrator(DaemonClient(sock))
            try:
                stream = ro.subscribe("s1")
                await ro._client.events_ready("s1")
                found = await ro.ensure_driver("s1")
                ev = await asyncio.wait_for(stream.__anext__(), 2)
            finally:
                await ro.aclose()
    assert found is True
    assert ev == {"type": "textDelta", "turnId": "t1", "text": "hi"}


@pytest.mark.asyncio
async def test_daemon_unavailable_propagates():
    ro = RemoteOrchestrator(DaemonClient("/nonexistent/ccsd.sock"))
    with pytest.raises(DaemonUnavailable):
        await ro.list_sessions()
    await ro.aclose()
