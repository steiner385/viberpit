"""The /vnc proxy must open the daemon WS via daemon_ws_connect (endpoint-aware).

Approach: monkeypatch server.bridge.vnc_ws.daemon_ws_connect with a fake, then
invoke the async handler *directly* (bypassing HTTP/WS plumbing) to confirm the
remote branch calls daemon_ws_connect with the expected Endpoint, path, and ssl ctx.

We use pytest-asyncio to drive async handler code without going through TestClient,
which can deadlock when the relay pump is waiting for client data that never arrives.
"""
from __future__ import annotations
import asyncio
import pytest

import server.bridge.vnc_ws as mod
from server.ipc.paths import Endpoint


class _FakeWS:
    """Minimal fake websockets connection: raises ConnectionClosed on recv so relay exits."""

    close_code: int | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def recv(self):
        from websockets.exceptions import ConnectionClosed
        raise ConnectionClosed(None, None)  # type: ignore[arg-type]

    async def send(self, data):
        pass

    async def close(self):
        pass


class _FakeBrowserWS:
    """Minimal stand-in for Starlette WebSocket (browser side of the relay)."""

    def __init__(self, session: str = "mysession"):
        self.query_params: dict[str, str] = {"session": session}
        self.accepted = False
        self.close_called: tuple[int, ...] = ()
        self._recv_queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def accept(self):
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = ""):
        self.close_called = (code,)

    async def send_bytes(self, data: bytes):
        # relay sends bytes to browser — we don't need them for this test
        pass

    async def receive_bytes(self) -> bytes:
        return await self._recv_queue.get()


def _extract_handler(router):
    """Pull the async handler coroutine out of the FastAPI router for direct invocation."""
    routes = router.routes
    assert routes, "router has no routes"
    return routes[0].endpoint


@pytest.mark.asyncio
async def test_remote_branch_calls_daemon_ws_connect_with_correct_args(monkeypatch):
    """The remote branch must call daemon_ws_connect(endpoint, path, ssl_ctx) — not inline unix_connect."""
    captured: dict = {}

    def _fake_connect(endpoint, path, ssl_ctx=None):
        captured["endpoint"] = endpoint
        captured["path"] = path
        captured["ssl"] = ssl_ctx
        return _FakeWS()

    monkeypatch.setattr(mod, "daemon_ws_connect", _fake_connect)

    ep = Endpoint("https", host="127.0.0.1", port=9443)
    router = mod.build_vnc_ws_router(
        get_x11=lambda: None,
        get_daemon_endpoint=lambda: ep,
        get_daemon_ssl=lambda: "SSLCTX",
    )

    handler = _extract_handler(router)
    browser_ws = _FakeBrowserWS(session="abc123")

    # Run the handler; the relay exits when _FakeWS.recv() raises ConnectionClosed.
    await handler(browser_ws, identity="test@example.com")

    assert captured, "daemon_ws_connect was never called — remote branch did not fire"
    assert captured["endpoint"] is ep, f"wrong endpoint: {captured['endpoint']!r}"
    assert captured["path"] == "/vnc?session=abc123", f"wrong path: {captured['path']!r}"
    assert captured["ssl"] == "SSLCTX", f"wrong ssl_ctx: {captured['ssl']!r}"
    assert browser_ws.accepted, "websocket was never accepted"


@pytest.mark.asyncio
async def test_local_branch_does_not_call_daemon_ws_connect(monkeypatch):
    """In-process branch: daemon_ws_connect must NOT be called when get_daemon_endpoint() is None."""
    called: list = []

    def _fake_connect(endpoint, path, ssl_ctx=None):
        called.append((endpoint, path, ssl_ctx))
        return _FakeWS()

    monkeypatch.setattr(mod, "daemon_ws_connect", _fake_connect)

    # x11 is None → handler closes with 4401 (not enabled) without touching daemon_ws_connect
    router = mod.build_vnc_ws_router(
        get_x11=lambda: None,
        get_daemon_endpoint=lambda: None,
        get_daemon_ssl=lambda: None,
    )

    handler = _extract_handler(router)
    browser_ws = _FakeBrowserWS(session="s")
    await handler(browser_ws, identity="test@example.com")

    assert not called, f"daemon_ws_connect must not be called in in-process mode; got: {called}"
    assert browser_ws.close_called == (mod.CLOSE_NOT_ENABLED,), (
        f"expected 4401 close, got: {browser_ws.close_called}"
    )
