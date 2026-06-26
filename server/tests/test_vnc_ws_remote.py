"""Edge /ws/vnc proxies to the daemon /vnc over the IPC socket in remote mode."""
import asyncio
import threading
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server.ipc.daemon import create_daemon_app
from server.ipc.paths import Endpoint
from server.ipc.x11_rpc import register_x11
from server.tests.test_x11_rpc import _FakeX11  # reuse the fake


class _DaemonOnUDS:
    """Run a bare daemon app (x11 only) on a real UDS in a background loop."""
    def __init__(self, x11):
        self._x11 = x11
        self.sock = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        import uvicorn, tempfile, os
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.sock = os.path.join(tempfile.mkdtemp(), "d.sock")
        app = create_daemon_app()
        register_x11(app, self._x11)

        async def _serve():
            config = uvicorn.Config(app, uds=self.sock, log_level="warning")
            server = uvicorn.Server(config)
            t = loop.create_task(server.serve())
            while not server.started:
                await asyncio.sleep(0.02)
            self._ready.set()
            while not self._stop.is_set():
                await asyncio.sleep(0.05)
            server.should_exit = True
            await t
        loop.run_until_complete(_serve())
        loop.close()

    def start(self):
        self._t.start()
        assert self._ready.wait(5)

    def stop(self):
        self._stop.set()
        self._t.join(5)


def _edge_router(get_sock):
    from fastapi import FastAPI
    from server.bridge.vnc_ws import build_vnc_ws_router
    from server.auth.middleware import require_ws_identity
    app = FastAPI()
    app.dependency_overrides[require_ws_identity] = lambda: "test@example.com"
    app.include_router(build_vnc_ws_router(
        lambda: None,
        get_daemon_endpoint=lambda: Endpoint("unix", path=get_sock()),
        get_daemon_ssl=lambda: None,
    ))
    return app


@pytest.fixture()
def echo_daemon():
    """A blocking-socket RFB echo server in a background thread (live, like x11vnc)."""
    import socket
    import threading

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
                conn.sendall(b"D:" + data)
            conn.close()
        except OSError:
            pass

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    yield port
    srv.close()
    t.join(timeout=2)


def test_edge_proxies_relay_and_4404(echo_daemon):
    # Display present → bytes round-trip through edge→daemon→local RFB echo.
    x11 = _FakeX11(has_display=True, port=echo_daemon)
    d = _DaemonOnUDS(x11)
    d.start()
    try:
        app = _edge_router(lambda: d.sock)
        with TestClient(app, raise_server_exceptions=False) as c:
            with c.websocket_connect("/ws/vnc?session=S") as ws:
                ws.send_bytes(b"PING")
                assert ws.receive_bytes() == b"D:PING"
    finally:
        d.stop()

    # No display → edge surfaces the daemon's 4404 close to the browser.
    d2 = _DaemonOnUDS(_FakeX11(has_display=False))
    d2.start()
    try:
        app = _edge_router(lambda: d2.sock)
        with TestClient(app, raise_server_exceptions=False) as c:
            with c.websocket_connect("/ws/vnc?session=S") as ws:
                with pytest.raises(WebSocketDisconnect) as ei:
                    ws.receive_bytes()
                assert ei.value.code == 4404
    finally:
        d2.stop()
