import asyncio
import pytest
from server.sessions.remote import RemoteDriver, RemoteOrchestrator


class _FakeClient:
    """Minimal client: subscribe() yields preset frames; call() returns found."""
    def __init__(self, frames, found=True):
        self._frames = frames
        self._found = found
        self.calls = []

    async def call(self, method, **params):
        self.calls.append((method, params))
        return {"found": self._found}

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
    assert got == [{"type": "textDelta", "text": "hi"}, {"type": "turnEnd"}]


@pytest.mark.asyncio
async def test_ensure_driver_returns_remotedriver_when_found():
    c = _FakeClient([{"type": "turnEnd"}], found=True)
    ro = RemoteOrchestrator(c)
    driver = await ro.ensure_driver("s1")
    assert isinstance(driver, RemoteDriver)
    assert c.calls == [("ensure_driver", {"session_id": "s1", "identity": "system"})]
    first = await asyncio.wait_for(driver.events().__anext__(), 2)
    assert first == {"type": "turnEnd"}


@pytest.mark.asyncio
async def test_ensure_driver_returns_none_when_not_found():
    c = _FakeClient([], found=False)
    ro = RemoteOrchestrator(c)
    assert await ro.ensure_driver("missing") is None
