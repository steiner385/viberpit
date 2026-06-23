"""Daemon-side x11 surface: /vnc WS close semantics + RPC handlers."""
import asyncio
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server.ipc.daemon import create_daemon_app
from server.ipc.x11_rpc import register_x11


class _Spec:  # truthy stand-in for a DisplaySpec
    pass


class _FakeX11:
    def __init__(self, *, has_display, port=None, launch_ok=True):
        self._has = has_display
        self._port = port
        self._launch_ok = launch_ok
        self.attached = 0
        self.detached = 0
        self.launched = []

    def display_for(self, sid):
        return _Spec() if self._has else None

    async def attach(self, sid):
        self.attached += 1
        return self._port

    def detach(self, sid):
        self.detached += 1

    def metrics(self):
        return {"displays_allocated": 1, "vnc_clients_attached": self.attached}

    async def launch_browser(self, sid, url):
        self.launched.append((sid, url))
        return self._launch_ok


def _app(x11):
    app = create_daemon_app()
    register_x11(app, x11)
    return app


def test_vnc_closes_4404_when_no_display():
    x11 = _FakeX11(has_display=False)
    with TestClient(_app(x11)) as c:
        with c.websocket_connect("/vnc?session=S") as ws:
            with pytest.raises(WebSocketDisconnect) as ei:
                ws.receive_bytes()
            assert ei.value.code == 4404
    assert x11.attached == 0


def test_vnc_relays_to_local_rfb_and_detaches():
    # A real loopback "RFB" echo server stands in for x11vnc.
    async def _run():
        async def handle(reader, writer):
            data = await reader.read(16)
            writer.write(b"SRV:" + data)
            await writer.drain()
            writer.close()
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        return server, port

    loop = asyncio.new_event_loop()
    server, port = loop.run_until_complete(_run())
    x11 = _FakeX11(has_display=True, port=port)
    try:
        with TestClient(_app(x11)) as c:
            with c.websocket_connect("/vnc?session=S") as ws:
                ws.send_bytes(b"HELLO")
                assert ws.receive_bytes() == b"SRV:HELLO"
        assert x11.attached == 1 and x11.detached == 1
    finally:
        server.close()
        loop.run_until_complete(server.wait_closed())
        loop.close()


@pytest.mark.asyncio
async def test_x11_metrics_rpc_returns_manager_metrics():
    x11 = _FakeX11(has_display=True)
    app = _app(x11)
    result = await app.state.rpc["x11_metrics"]({})
    assert result["displays_allocated"] == 1


@pytest.mark.asyncio
async def test_x11_open_browser_rpc():
    x11 = _FakeX11(has_display=True, launch_ok=True)
    app = _app(x11)
    ok = await app.state.rpc["x11_open_browser"]({"session_id": "S", "url": "https://x"})
    assert ok == {"ok": True, "reason": None}
    assert x11.launched == [("S", "https://x")]

    no = await _app(_FakeX11(has_display=False)).state.rpc["x11_open_browser"]({"session_id": "S"})
    assert no == {"ok": False, "reason": "no_display"}
