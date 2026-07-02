# server/tests/test_ws_gap_healing.py
import asyncio
import pytest
from server.bridge.ws import _forward_live, _redact_buffer


class _FakeEvent:
    def __init__(self, wire): self._wire = wire
    def to_wire(self): return dict(self._wire)


class _BoomEvent:
    def to_wire(self): raise RuntimeError("bad event")


class _FakeDriver:
    def __init__(self, events): self._events = events
    async def events(self):
        for e in self._events:
            yield e


@pytest.mark.asyncio
async def test_forward_live_stamps_seq_and_session_id():
    out_q: asyncio.Queue = asyncio.Queue()
    driver = _FakeDriver([_FakeEvent({"type": "textDelta", "text": "a"}),
                          _FakeEvent({"type": "textDelta", "text": "b"})])
    await _forward_live(driver, out_q, "sess-1")
    first = out_q.get_nowait()
    second = out_q.get_nowait()
    assert first["sessionId"] == "sess-1" and first["seq"] == 0
    assert second["sessionId"] == "sess-1" and second["seq"] == 1


@pytest.mark.asyncio
async def test_forward_live_gap_on_to_wire_failure():
    """A single bad event must not kill the stream, and must leave a seq hole
    so the client self-heals."""
    out_q: asyncio.Queue = asyncio.Queue()
    driver = _FakeDriver([_FakeEvent({"type": "textDelta", "text": "a"}),
                          _BoomEvent(),
                          _FakeEvent({"type": "textDelta", "text": "c"})])
    await _forward_live(driver, out_q, "sess-1")
    got = [out_q.get_nowait(), out_q.get_nowait()]
    assert out_q.empty()
    assert [e["seq"] for e in got] == [0, 2]  # seq 1 was consumed by the failed event


def test_redact_buffer_drops_unredactable_without_raising(monkeypatch):
    import server.bridge.ws as wsmod
    good = {"type": "textDelta", "text": "ok", "seq": 0}
    bad = {"type": "textDelta", "text": "boom", "seq": 1}

    def fake_redact(e):
        if e["text"] == "boom":
            raise RuntimeError("redact blew up")
        return e

    monkeypatch.setattr(wsmod, "redact_event", fake_redact)
    out = wsmod._redact_buffer([good, bad])
    assert out == [good]  # bad dropped, no exception, frame still flows
