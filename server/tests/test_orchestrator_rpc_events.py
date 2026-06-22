import json
from unittest import mock
from fastapi.testclient import TestClient
from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.sessions.orchestrator import SessionOrchestrator


class _FakeEvent:
    def __init__(self, i): self.i = i
    def to_wire(self): return {"type": "textDelta", "turnId": "t1", "i": self.i}


class _FakeDriver:
    async def events(self):
        for i in range(3):
            yield _FakeEvent(i)


def test_ensure_driver_pumps_events_to_subscriber():
    app = create_daemon_app()
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda name: None)
    register_orchestrator_rpc(app, orch)
    with mock.patch.object(orch, "ensure_driver",
                           new=mock.AsyncMock(return_value=_FakeDriver())):
        with TestClient(app) as c:
            with c.websocket_connect("/events") as ws:
                ws.send_text(json.dumps({"type": "subscribe", "stream": "s1"}))
                found = c.post("/rpc/ensure_driver", json={"session_id": "s1"}).json()
                got = [json.loads(ws.receive_text()) for _ in range(3)]
    assert found["result"] == {"found": True}
    assert [f["event"]["i"] for f in got] == [0, 1, 2]
    assert all(f["stream"] == "s1" for f in got)


def test_ensure_driver_not_found_returns_false():
    app = create_daemon_app()
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda name: None)
    register_orchestrator_rpc(app, orch)
    with mock.patch.object(orch, "ensure_driver", new=mock.AsyncMock(return_value=None)):
        with TestClient(app) as c:
            r = c.post("/rpc/ensure_driver", json={"session_id": "x"}).json()
    assert r["result"] == {"found": False}
