"""Unit test for the _relay bidirectional pump in vnc_ws.py."""
import asyncio
import pytest
from server.bridge import vnc_ws
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


@pytest.mark.asyncio
async def test_open_rfb_connection_retries_until_bound(monkeypatch):
    """The relay connect retries on ConnectionRefused while x11vnc is still
    binding its RFB port, so the first attach isn't lost (issue #135). ensure_vnc
    already waits for the bind, but that wait is best-effort — under load it can
    return an unbound port, and a single connect would then race the bind."""
    attempts = {"n": 0}
    sentinel = (object(), object())

    async def flaky_open(host, port):
        assert (host, port) == ("127.0.0.1", 6100)
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionRefusedError(111, "Connection refused")
        return sentinel

    monkeypatch.setattr(vnc_ws.asyncio, "open_connection", flaky_open)
    result = await vnc_ws._open_rfb_connection(6100, timeout=1.0, interval=0.01)
    assert result == sentinel
    assert attempts["n"] == 3      # refused twice, connected on the third try


@pytest.mark.asyncio
async def test_open_rfb_connection_gives_up_after_timeout(monkeypatch):
    """If x11vnc never binds, the retry budget is bounded and the final
    ConnectionRefused propagates (the relay then logs and detaches, unchanged)."""
    attempts = {"n": 0}

    async def always_refused(host, port):
        attempts["n"] += 1
        raise ConnectionRefusedError(111, "Connection refused")

    monkeypatch.setattr(vnc_ws.asyncio, "open_connection", always_refused)
    with pytest.raises(ConnectionRefusedError):
        await vnc_ws._open_rfb_connection(6100, timeout=0.05, interval=0.01)
    assert attempts["n"] >= 2      # retried at least once before giving up
