import pytest
from server.reauth import ReauthManager, ReauthState, ReauthBusy
from server.reauth_remote import RemoteReauthManager, InProcessReauthAdapter
from server.ipc.client import DaemonError


class _FakeClient:
    def __init__(self, responses=None, error=None):
        self.responses = responses or {}
        self.error = error
        self.calls = []
    async def call(self, method, **params):
        self.calls.append((method, params))
        if self.error:
            raise self.error
        return self.responses.get(method, {"state": "running", "vncSession": "__reauth__"})


@pytest.mark.asyncio
async def test_remote_start_returns_state():
    c = _FakeClient()
    mgr = RemoteReauthManager(c)
    s = await mgr.start("primary", "me@example.com")
    assert s.state == "running" and s.vnc_session == "__reauth__"
    assert c.calls[0] == ("reauth_start", {"name": "primary", "identity": "me@example.com"})


@pytest.mark.asyncio
async def test_remote_reraises_busy_by_type():
    c = _FakeClient(error=DaemonError("ReauthBusy", "reauth busy"))
    mgr = RemoteReauthManager(c)
    with pytest.raises(ReauthBusy):
        await mgr.start("primary", "me@example.com")


@pytest.mark.asyncio
async def test_remote_status_and_cancel():
    c = _FakeClient(responses={"reauth_status": {"state": "running", "vncSession": "__reauth__"},
                               "reauth_cancel": {"state": "idle", "message": "cancelled"}})
    mgr = RemoteReauthManager(c)
    assert (await mgr.status("primary")).vnc_session == "__reauth__"
    assert (await mgr.cancel("primary")).state == "idle"


@pytest.mark.asyncio
async def test_inprocess_adapter_wraps_sync_status():
    real = ReauthManager(key_available=lambda: True)
    adapter = InProcessReauthAdapter(real)
    s = await adapter.status("primary")        # sync status, awaited
    assert isinstance(s, ReauthState)
