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


@pytest.mark.asyncio
async def test_create_session_retries_without_display_kwargs_on_old_daemon(caplog):
    from server.ipc.client import DaemonError
    class _Old:
        def __init__(self): self.calls = []
        async def call(self, method, **params):
            self.calls.append((method, params))
            if "client_node" in params:
                raise DaemonError("TypeError",
                                  "create_session() got an unexpected keyword argument 'client_node'")
            return {"session": {"id": "s1", "name": "n", "account": "a", "engine": "claude_code",
                    "engineSessionId": "", "cols": 80, "rows": 24, "cwd": "/tmp"}}
    c = _Old()
    rec = await RemoteOrchestrator(c).create_session(name="n", client_node="dobby", display="auto")
    assert rec.id == "s1"
    assert len(c.calls) == 2 and "client_node" not in c.calls[1][1] and "display" not in c.calls[1][1]
    assert "predates display routing" in caplog.text
