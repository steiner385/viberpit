"""_WSAdapter lets the _relay byte-pump speak to a `websockets` client connection."""
import pytest
from server.bridge.vnc_ws import _WSAdapter


class _FakeClientWS:
    def __init__(self, incoming):
        self._in = list(incoming)
        self.sent = []

    async def recv(self):
        if self._in:
            return self._in.pop(0)
        from websockets.exceptions import ConnectionClosedOK
        raise ConnectionClosedOK(None, None)

    async def send(self, b):
        self.sent.append(b)


@pytest.mark.asyncio
async def test_read_returns_bytes_and_eof_on_close():
    ws = _FakeClientWS([b"RFB 003.008\n", "text-frame"])
    a = _WSAdapter(ws)
    assert await a.read(65536) == b"RFB 003.008\n"
    assert await a.read(65536) == b"text-frame"     # str frame coerced to bytes
    assert await a.read(65536) == b""                # ConnectionClosed → EOF sentinel


@pytest.mark.asyncio
async def test_write_then_drain_sends():
    ws = _FakeClientWS([])
    a = _WSAdapter(ws)
    a.write(b"\x00\x01")
    await a.drain()
    assert ws.sent == [b"\x00\x01"]
