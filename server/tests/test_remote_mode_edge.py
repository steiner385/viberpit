# server/tests/test_remote_mode_edge.py
import asyncio
import os
import threading
from unittest import mock

import pytest
from starlette.testclient import TestClient

from server.app import create_app
from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord
from server.tests.ipc_harness import serve_daemon_uds

_IDENT = {"Tailscale-User-Login": "edge@example.com"}


def _rec(sid: str, status: str = "idle") -> SessionRecord:
    r = SessionRecord(id=sid, name=sid.upper(), account="acct",
                      engine=AgentEngine.CLAUDE_CODE,
                      engine_session_id="cs-" + sid, cols=80, rows=24, cwd="/tmp")
    r.status = status
    return r


class _DaemonThread:
    """Run a daemon app on a real UDS in a background thread/loop. Real socket =
    the edge (its own loop) can reach it cross-thread, as in production."""

    def __init__(self, orch):
        self._orch = orch
        self.sock = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._err = None
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _serve():
            dapp = create_daemon_app()
            register_orchestrator_rpc(dapp, self._orch)  # running loop → pump starts
            from server.ipc.x11_rpc import register_x11
            register_x11(dapp, getattr(self._orch, "_x11", None))
            async with serve_daemon_uds(dapp) as sock:
                self.sock = sock
                self._ready.set()
                while not self._stop.is_set():
                    await asyncio.sleep(0.05)

        try:
            loop.run_until_complete(_serve())
        except Exception as exc:  # noqa: BLE001 — surface to the main thread
            self._err = exc
            self._ready.set()
        finally:
            # Cancel any background daemon tasks (e.g. the orchestrator global-pump)
            # and let them settle before closing the loop, else a still-scheduled
            # task raises "Event loop is closed" on GC (PytestUnraisableException).
            pending = asyncio.all_tasks(loop)
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.close()

    def start(self):
        self._t.start()
        if not self._ready.wait(5):
            raise RuntimeError("daemon thread did not start")
        if self._err is not None:
            raise self._err

    def stop(self):
        self._stop.set()
        self._t.join(5)


@pytest.fixture()
def remote_edge(tmp_path):
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda n: None)
    daemon = _DaemonThread(orch)
    daemon.start()
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'edge.db'}"
    env = {
        "CCSWITCH_SESSIOND_SOCKET": daemon.sock,
        "CCSWITCH_DB_URL": db_url,
        "CCSWITCH_TAILNET_ALLOWLIST": _IDENT["Tailscale-User-Login"],
    }
    try:
        with mock.patch.dict(os.environ, env):
            app = create_app()
            with TestClient(app, raise_server_exceptions=False) as client:
                yield orch, client
    finally:
        daemon.stop()


@pytest.fixture()
def dead_socket_edge(tmp_path):
    """Edge in remote mode pointed at a socket with no daemon → every RPC raises
    DaemonUnavailable."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'edge.db'}"
    env = {
        "CCSWITCH_SESSIOND_SOCKET": str(tmp_path / "dead.sock"),
        "CCSWITCH_DB_URL": db_url,
        "CCSWITCH_TAILNET_ALLOWLIST": _IDENT["Tailscale-User-Login"],
    }
    with mock.patch.dict(os.environ, env):
        app = create_app()
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client


def test_healthz_busy_count_over_rpc(remote_edge):
    orch, client = remote_edge
    # Two active sessions on the daemon → edge healthz reflects them over RPC.
    orch._records["a"] = _rec("a", status="in_progress")
    orch._records["b"] = _rec("b", status="in_progress")
    resp = client.get("/healthz")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["activeSessions"] == 2
    assert body["busy"] is True
    assert body["daemon"] == "ok"


def test_rest_call_returns_503_when_daemon_down(dead_socket_edge):
    # GET /api/sessions → facade.list_sessions() → DaemonUnavailable → 503.
    resp = dead_socket_edge.get("/api/sessions", headers=_IDENT)
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["code"] == "daemon_unavailable"
    assert "restarting" in body["detail"]


import json as _json
import time as _time
from starlette.websockets import WebSocketDisconnect


def test_ws_connect_closes_1013_when_daemon_down(dead_socket_edge):
    # Attaching to a session triggers ensure_driver() → DaemonUnavailable.
    # The guard must close 1013 (try again later), not crash the handler.
    with pytest.raises(WebSocketDisconnect) as exc:
        with dead_socket_edge.websocket_connect("/ws", headers=_IDENT) as ws:
            ws.send_text(_json.dumps({"type": "attach", "sessionId": "s1"}))
            ws.receive_text()  # forces the server frames to be read
    assert exc.value.code == 1013


def test_rest_list_and_get_round_trip(remote_edge):
    orch, client = remote_edge
    orch._records["s1"] = _rec("s1")
    listed = client.get("/api/sessions", headers=_IDENT)
    assert listed.status_code == 200, listed.text
    assert any(s["id"] == "s1" for s in listed.json())
    got = client.get("/api/sessions/s1", headers=_IDENT)
    assert got.status_code == 200, got.text
    assert got.json()["id"] == "s1"


class _FakeEvent:
    def to_wire(self):
        return {"type": "textDelta", "turnId": "t1", "text": "hi", "sessionId": "s1"}


class _FakeDriver:
    # Registered in orch._drivers (detached derives from that), so the live-attach
    # extras consult it like any live driver: nothing pending, no catalog, no seeding.
    def pending_questions(self):
        return []

    def pending_permissions(self):
        return []

    async def server_info(self):
        return None

    def seed_idle_suggestions(self, *a, **k):
        return None

    def current_status(self):
        return "idle"

    def queued(self):
        return []

    def open_stream(self):
        # ws.py consumes drivers through the uniform open_stream() interface
        # (turn flags pinned to the stream, no hasattr branching) — mirror the
        # RemoteDriverStream shape.
        driver = self

        class _Stream:
            turn_active = False
            turn_started_at = None

            def events(self):
                return driver.events()

            def close(self):
                pass

        return _Stream()

    async def events(self):
        # Delay emission so the edge client can subscribe to the daemon channel
        # before we publish — avoids a race where the single event is lost.
        await asyncio.sleep(0.5)
        yield _FakeEvent()


def test_ws_attach_streams_event_from_daemon(remote_edge):
    orch, client = remote_edge
    orch._records["s1"] = _rec("s1")
    driver = _FakeDriver()
    # The real ensure_driver registers the driver it returns; `detached` is DERIVED from
    # that registration (no driver → read-only replay attach), so the mock must mirror it.
    orch._drivers["s1"] = driver
    with mock.patch.object(orch, "ensure_driver",
                           new=mock.AsyncMock(return_value=driver)), \
         mock.patch.object(orch, "transcript_path", new=mock.AsyncMock(return_value=None)):
        with client.websocket_connect("/ws", headers=_IDENT) as ws:
            ws.send_json({"type": "attach", "sessionId": "s1"})
            # Drain frames until the streamed textDelta arrives (batch-wrapped).
            deadline = _time.time() + 5
            seen = None
            while _time.time() < deadline and seen is None:
                msg = ws.receive_json()
                events = msg.get("events", []) if msg.get("type") == "batch" else [msg]
                for e in events:
                    if e.get("type") == "textDelta" and e.get("text") == "hi":
                        seen = e
                        break
            assert seen is not None, "did not receive streamed textDelta"


def test_rest_daemonerror_maps_to_409(remote_edge):
    orch, client = remote_edge
    from server.ipc.client import DaemonError

    async def boom(*a, **k):
        raise DaemonError("KeyError", "account")

    client.app.state.sessions.list_sessions = boom
    resp = client.get("/api/sessions", headers=_IDENT)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "account"


def test_vnc_end_to_end_remote(tmp_path):
    """browser → edge /ws/vnc → daemon /vnc → local RFB echo → browser, in real remote mode."""
    import socket
    import threading
    from server.tests.test_x11_rpc import _FakeX11

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def _serve():
        try:
            conn, _ = srv.accept()
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                conn.sendall(b"E:" + data)
            conn.close()
        except OSError:
            pass

    t = threading.Thread(target=_serve, daemon=True)
    t.start()

    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda n: None)
    orch._x11 = _FakeX11(has_display=True, port=port)
    daemon = _DaemonThread(orch)
    daemon.start()
    env = {
        "CCSWITCH_SESSIOND_SOCKET": daemon.sock,
        "CCSWITCH_DB_URL": f"sqlite+aiosqlite:///{tmp_path / 'edge.db'}",
        "CCSWITCH_TAILNET_ALLOWLIST": _IDENT["Tailscale-User-Login"],
    }
    try:
        with mock.patch.dict(os.environ, env):
            app = create_app()
            assert app.state.x11 is None  # remote mode: no local manager
            with TestClient(app, raise_server_exceptions=False) as client:
                with client.websocket_connect("/ws/vnc?session=S", headers=_IDENT) as ws:
                    ws.send_bytes(b"HI")
                    assert ws.receive_bytes() == b"E:HI"
                m = client.get("/api/x11/metrics", headers=_IDENT)
                assert m.status_code == 200 and m.json().get("displays_allocated") == 1
    finally:
        daemon.stop()
        srv.close()
        t.join(timeout=2)
