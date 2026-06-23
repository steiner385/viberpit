"""Unit test for the _relay bidirectional pump in vnc_ws.py."""
import asyncio
import pytest
from server.bridge.vnc_ws import _relay  # bytes pump used by the route


class FakeWS:
    def __init__(self, incoming):
        self._in = list(incoming)
        self.sent = []
        self.closed = None

    async def receive_bytes(self):
        if self._in:
            return self._in.pop(0)
        raise StopAsyncIteration

    async def send_bytes(self, b):
        self.sent.append(b)

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)


@pytest.mark.asyncio
async def test_relay_pumps_both_directions():
    # Fake TCP endpoint: echoes what the WS sent, plus a server-initiated frame.
    srv_to_ws = asyncio.Queue()
    await srv_to_ws.put(b"RFB 003.008\n")

    class FakeTCP:
        def __init__(self):
            self.written = []

        async def read(self, n):
            try:
                return await asyncio.wait_for(srv_to_ws.get(), 0.1)
            except asyncio.TimeoutError:
                return b""

        def write(self, b):
            self.written.append(b)

        async def drain(self):
            ...

        async def close(self):
            ...

    ws = FakeWS([b"\x00\x01"])
    tcp = FakeTCP()
    await _relay(ws, tcp, idle=0.05)
    assert b"RFB 003.008\n" in ws.sent          # server→client
    assert b"\x00\x01" in tcp.written            # client→server
