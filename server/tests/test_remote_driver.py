import asyncio
import pytest
from server.sessions.remote import RemoteDriver, RemoteOrchestrator


class _FakeClient:
    """Minimal client: subscribe() yields preset frames; call() returns found."""
    def __init__(self, frames, found=True, status="idle", queued=None):
        self._frames = frames
        self._found = found
        self._status = status
        self._queued = queued or []
        self.calls = []

    async def call(self, method, **params):
        self.calls.append((method, params))
        if self._found:
            return {"found": self._found, "status": self._status, "queued": self._queued}
        return {"found": False}

    async def _gen(self):
        for f in self._frames:
            yield f

    def subscribe(self, session_id):
        return self._gen()


@pytest.mark.asyncio
async def test_remote_driver_events_wrap_subscribe():
    c = _FakeClient([{"type": "textDelta", "text": "hi"}, {"type": "turnEnd"}])
    d = RemoteDriver(c, "s1")
    got = [e async for e in d.events()]
    assert [e.to_wire() for e in got] == [{"type": "textDelta", "text": "hi"}, {"type": "turnEnd"}]


@pytest.mark.asyncio
async def test_ensure_driver_returns_remotedriver_when_found():
    c = _FakeClient([{"type": "turnEnd"}], found=True, status="idle", queued=[])
    ro = RemoteOrchestrator(c)
    driver = await ro.ensure_driver("s1")
    assert isinstance(driver, RemoteDriver)
    assert c.calls == [("ensure_driver", {"session_id": "s1", "identity": "system"})]
    first = await asyncio.wait_for(driver.events().__anext__(), 2)
    assert first.to_wire() == {"type": "turnEnd"}


@pytest.mark.asyncio
async def test_ensure_driver_returns_none_when_not_found():
    c = _FakeClient([], found=False)
    ro = RemoteOrchestrator(c)
    assert await ro.ensure_driver("missing") is None


@pytest.mark.asyncio
async def test_remote_driver_snapshot_and_wire_events():
    from server.sessions.remote import RemoteDriver

    class _Client:
        async def call(self, method, **params):
            return {"found": True, "status": "in_progress", "queued": ["q1", "q2"]}
        async def _gen(self):
            yield {"type": "textDelta", "text": "hi"}
        def subscribe(self, session_id):
            return self._gen()

    ro = RemoteOrchestrator(_Client())
    d = await ro.ensure_driver("s1")
    assert isinstance(d, RemoteDriver)
    assert d.current_status() == "in_progress"
    assert d.queued() == ["q1", "q2"]
    ev = await d.events().__anext__()
    assert ev.to_wire() == {"type": "textDelta", "text": "hi"}
