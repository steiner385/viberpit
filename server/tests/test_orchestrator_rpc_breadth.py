from unittest import mock
from fastapi.testclient import TestClient
from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord


def _rec(sid="s1"):
    return SessionRecord(id=sid, name="S", account="a", claude_session_id="cs",
                         cols=80, rows=24, cwd="/tmp")


def _app():
    app = create_daemon_app()
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda n: None)
    register_orchestrator_rpc(app, orch)
    return app, orch


def test_simple_returns_wrapped_in_result():
    app, orch = _app()
    orch.resolve_permission = mock.MagicMock(return_value=True)        # sync, bool
    orch.permanent_delete = mock.AsyncMock(return_value=False)         # async, bool
    orch.archive_session = mock.AsyncMock(return_value=None)           # async, None
    orch.transcript_path = mock.MagicMock(return_value="/p/t.jsonl")   # sync, str
    with TestClient(app) as c:
        # Daemon wraps handler return as {"result": <value>}; simple handlers return the raw value.
        assert c.post("/rpc/resolve_permission", json={"session_id": "s1", "perm_id": "p", "decision": "allow", "identity": "me"}).json()["result"] is True
        assert c.post("/rpc/permanent_delete", json={"session_id": "s1"}).json()["result"] is False
        assert c.post("/rpc/archive_session", json={"session_id": "s1"}).json()["result"] is None
        assert c.post("/rpc/transcript_path", json={"session_id": "s1"}).json()["result"] == "/p/t.jsonl"
    orch.resolve_permission.assert_called_once_with(session_id="s1", perm_id="p", decision="allow", identity="me")


def test_unarchive_returns_session_shape():
    app, orch = _app()
    orch.unarchive = mock.AsyncMock(return_value=_rec("u1"))
    with TestClient(app) as c:
        # Daemon wraps handler return as {"result": {"session": {...}}}
        r = c.post("/rpc/unarchive", json={"session_id": "u1"}).json()
    assert r["result"]["session"]["id"] == "u1"


def test_unarchive_none():
    app, orch = _app()
    orch.unarchive = mock.AsyncMock(return_value=None)
    with TestClient(app) as c:
        assert c.post("/rpc/unarchive", json={"session_id": "x"}).json()["result"]["session"] is None


def test_adopt_session_unpacks_tuple():
    app, orch = _app()
    orch.adopt_session = mock.AsyncMock(return_value=(_rec("a1"), {"resumed": True, "terminatedPid": None}))
    with TestClient(app) as c:
        # Daemon wraps handler return as {"result": {"record": {...}, "metadata": {...}}}
        r = c.post("/rpc/adopt_session", json={"claude_session_id": "cs", "identity": "me"}).json()
    assert r["result"]["record"]["id"] == "a1"
    assert r["result"]["metadata"] == {"resumed": True, "terminatedPid": None}


def test_seed_idle_suggestions_routes_to_driver():
    # The orchestrator method (invoked over RPC by RemoteDriver) must forward to
    # the live driver's seed_idle_suggestions for the given session.
    _, orch = _app()
    drv = mock.MagicMock()
    orch._drivers["s1"] = drv
    assert orch.seed_idle_suggestions(session_id="s1", user_text="u", assistant_text="a") is None
    drv.seed_idle_suggestions.assert_called_once_with("u", "a")


def test_seed_idle_suggestions_noop_without_driver():
    # No live driver for the session → best-effort no-op, never raises.
    _, orch = _app()
    assert orch.seed_idle_suggestions(session_id="missing", user_text="u", assistant_text="a") is None


def test_seed_idle_suggestions_registered_as_rpc():
    app, orch = _app()
    orch.seed_idle_suggestions = mock.MagicMock(return_value=None)
    with TestClient(app) as c:
        r = c.post("/rpc/seed_idle_suggestions",
                   json={"session_id": "s1", "user_text": "u", "assistant_text": "a"}).json()
    assert r["result"] is None
    orch.seed_idle_suggestions.assert_called_once_with(session_id="s1", user_text="u", assistant_text="a")


def test_transcript_events_to_wire():
    app, orch = _app()

    class _Ev:
        def to_wire(self): return {"type": "textDelta", "text": "x"}

    orch.transcript_events = mock.AsyncMock(return_value=[_Ev(), _Ev()])
    with TestClient(app) as c:
        # Daemon wraps handler return as {"result": {"events": [...]}}
        r = c.post("/rpc/transcript_events", json={"session_id": "s1"}).json()
    assert r["result"]["events"] == [{"type": "textDelta", "text": "x"}, {"type": "textDelta", "text": "x"}]


def test_live_session_load_registered_as_rpc():
    app, orch = _app()
    orch.live_session_load = mock.MagicMock(return_value={"acct-a": 0.5, "acct-b": 1.0})
    with TestClient(app) as c:
        r = c.post("/rpc/live_session_load", json={}).json()
    assert isinstance(r["result"], dict)
    orch.live_session_load.assert_called_once_with()


def test_live_session_counts_registered_as_rpc():
    app, orch = _app()
    orch.live_session_counts = mock.MagicMock(
        return_value={"acct-a": {"active": 2, "idle": 1}, "acct-b": {"active": 0, "idle": 0}}
    )
    with TestClient(app) as c:
        r = c.post("/rpc/live_session_counts", json={}).json()
    assert isinstance(r["result"], dict)
    orch.live_session_counts.assert_called_once_with()
