"""RemoteOrchestrator x11 facade methods proxy straight to daemon RPCs."""
import pytest
from server.sessions.remote import RemoteOrchestrator


class _FakeClient:
    def __init__(self):
        self.calls = []

    async def call(self, method, **params):
        self.calls.append((method, params))
        if method == "x11_metrics":
            return {"displays_allocated": 2}
        if method == "x11_open_browser":
            return {"ok": True, "reason": None}
        raise AssertionError(method)


@pytest.mark.asyncio
async def test_x11_metrics_proxies():
    c = _FakeClient()
    orch = RemoteOrchestrator(c)
    assert (await orch.x11_metrics())["displays_allocated"] == 2
    assert c.calls == [("x11_metrics", {})]


@pytest.mark.asyncio
async def test_x11_open_browser_proxies():
    c = _FakeClient()
    orch = RemoteOrchestrator(c)
    res = await orch.x11_open_browser("S", "https://x")
    assert res == {"ok": True, "reason": None}
    assert c.calls == [("x11_open_browser", {"session_id": "S", "url": "https://x"})]
