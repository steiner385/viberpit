from unittest import mock
import pytest
from server.ipc.client import DaemonClient
from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.remote import RemoteOrchestrator
from server.sessions.types import SessionRecord
from server.tests.ipc_harness import serve_daemon_uds


def _rec(sid="s1"):
    return SessionRecord(id=sid, name="S", account="a", claude_session_id="cs",
                         cols=80, rows=24, cwd="/tmp")


def _build():
    app = create_daemon_app()
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda n: None)
    register_orchestrator_rpc(app, orch)
    return app, orch


@pytest.mark.asyncio
async def test_simple_and_quirk_methods_round_trip():
    app, orch = _build()
    orch.permanent_delete = mock.AsyncMock(return_value=True)
    orch.resolve_permission = mock.MagicMock(return_value=False)
    orch.answer_question = mock.MagicMock(return_value=True)
    orch.rename_session = mock.AsyncMock(return_value=None)
    orch.list_archived = mock.AsyncMock(return_value=[{"id": "old"}])
    orch.unarchive = mock.AsyncMock(return_value=_rec("u1"))
    orch.adopt_session = mock.AsyncMock(return_value=(_rec("a1"), {"resumed": True, "terminatedPid": 7}))

    class _Ev:
        def to_wire(self): return {"type": "tick"}
    orch.transcript_events = mock.AsyncMock(return_value=[_Ev()])

    async with serve_daemon_uds(app) as sock:
        ro = RemoteOrchestrator(DaemonClient(sock))
        try:
            assert await ro.permanent_delete("s1") is True
            assert await ro.resolve_permission("s1", "p", "deny", "me") is False
            assert await ro.answer_question("s1", "q", ["opt"], "me") is True
            assert await ro.rename_session("s1", "New") is None
            assert await ro.list_archived() == [{"id": "old"}]
            un = await ro.unarchive("u1")
            assert un.id == "u1"
            rec, meta = await ro.adopt_session("cs", "me")
            assert rec.id == "a1" and meta == {"resumed": True, "terminatedPid": 7}
            assert await ro.transcript_events("s1") == [{"type": "tick"}]
        finally:
            await ro.aclose()
    orch.resolve_permission.assert_called_once_with(session_id="s1", perm_id="p", decision="deny", identity="me")
