from unittest import mock
from fastapi.testclient import TestClient
from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord


def _rec(sid="s1"):
    return SessionRecord(id=sid, name="S1", account="acct", claude_session_id="cs",
                         cols=80, rows=24, cwd="/tmp")


def _app_with_orch():
    app = create_daemon_app()
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda name: None)
    register_orchestrator_rpc(app, orch)
    return app, orch


def test_list_and_get_sessions():
    app, orch = _app_with_orch()
    orch._records["s1"] = _rec("s1")
    with TestClient(app) as c:
        listed = c.post("/rpc/list_sessions", json={}).json()["result"]["sessions"]
        got = c.post("/rpc/get_session", json={"session_id": "s1"}).json()["result"]["session"]
        missing = c.post("/rpc/get_session", json={"session_id": "nope"}).json()["result"]["session"]
    assert [s["id"] for s in listed] == ["s1"]
    assert got["id"] == "s1"
    assert missing is None


def test_create_session_serializes_result():
    app, orch = _app_with_orch()
    with mock.patch.object(orch, "create_session",
                           new=mock.AsyncMock(return_value=_rec("new1"))) as cs:
        with TestClient(app) as c:
            r = c.post("/rpc/create_session", json={"name": "S", "account": "acct"}).json()
        cs.assert_awaited_once()
        assert cs.await_args.kwargs == {"name": "S", "account": "acct"}
    assert r["result"]["session"]["id"] == "new1"


def test_send_prompt_forwards_to_orch():
    app, orch = _app_with_orch()
    with mock.patch.object(orch, "send_prompt", new=mock.AsyncMock()) as sp:
        with TestClient(app) as c:
            r = c.post("/rpc/send_prompt", json={"session_id": "s1", "text": "hi"}).json()
        sp.assert_awaited_once_with("s1", "hi", None)
    assert r["result"] == {"ok": True}
