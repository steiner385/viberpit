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
    assert c.calls[0] == ("reauth_start", {"name": "primary", "identity": "me@example.com", "mode": "login"})


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


# ---------------------------------------------------------------------------
# B2c: submit_code + mode forwarding tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inprocess_adapter_has_submit_code():
    """InProcessReauthAdapter.submit_code delegates to the inner manager."""
    real = ReauthManager(key_available=lambda: True)
    adapter = InProcessReauthAdapter(real)
    # state is idle — submit_code is a no-op but must not raise
    snap = await adapter.submit_code("primary", "TESTCODE")
    assert isinstance(snap, ReauthState)
    assert snap.state == "idle"


@pytest.mark.asyncio
async def test_inprocess_adapter_start_passes_mode():
    """InProcessReauthAdapter.start forwards mode= to the inner manager."""
    modes_seen: list[str] = []

    class _CapturingManager:
        async def start(self, name, identity, mode="login"):
            modes_seen.append(mode)
            from server.reauth import ReauthState
            return ReauthState(state="running")

    adapter = InProcessReauthAdapter(_CapturingManager())
    await adapter.start("primary", "me@x", mode="setup_token")
    assert modes_seen == ["setup_token"], f"mode not forwarded, got {modes_seen}"


@pytest.mark.asyncio
async def test_remote_submit_code_calls_rpc():
    """RemoteReauthManager.submit_code calls reauth_submit_code RPC."""
    c = _FakeClient(responses={
        "reauth_submit_code": {"state": "running", "identity": "me@x.com"},
    })
    mgr = RemoteReauthManager(c)
    s = await mgr.submit_code("primary", "MYCODE")
    assert isinstance(s, ReauthState)
    assert c.calls[-1] == ("reauth_submit_code", {"name": "primary", "code": "MYCODE"})


@pytest.mark.asyncio
async def test_remote_start_passes_mode():
    """RemoteReauthManager.start forwards mode= to the RPC call."""
    c = _FakeClient()
    mgr = RemoteReauthManager(c)
    await mgr.start("primary", "me@example.com", mode="setup_token")
    # The RPC call params must include mode="setup_token"
    assert c.calls[0] == (
        "reauth_start",
        {"name": "primary", "identity": "me@example.com", "mode": "setup_token"},
    )
