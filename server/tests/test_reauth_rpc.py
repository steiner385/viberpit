import pytest
from server.ipc.daemon import create_daemon_app
from server.ipc.reauth_rpc import register_reauth
from server.reauth import ReauthState


class _FakeReauth:
    def __init__(self):
        self.calls = []
    async def start(self, name, identity, mode="login"):
        self.calls.append(("start", name, identity, mode))
        return ReauthState(state="running", identity=identity, vnc_session="__reauth__")
    def status(self, name):
        return ReauthState(state="running", vnc_session="__reauth__")
    async def cancel(self, name):
        self.calls.append(("cancel", name))
        return ReauthState(state="idle", message="cancelled")
    async def submit_code(self, name, code):
        self.calls.append(("submit_code", name, code))
        return ReauthState(state="running")


@pytest.mark.asyncio
async def test_register_reauth_dispatches():
    app = create_daemon_app()
    fake = _FakeReauth()
    register_reauth(app, fake)
    start = app.state.rpc["reauth_start"]
    status = app.state.rpc["reauth_status"]
    cancel = app.state.rpc["reauth_cancel"]

    r = await start({"name": "primary", "identity": "me@example.com"})
    assert r["state"] == "running" and r["vncSession"] == "__reauth__"
    assert ("start", "primary", "me@example.com", "login") in fake.calls

    s = await status({"name": "primary"})
    assert s["state"] == "running"

    c = await cancel({"name": "primary"})
    assert c["state"] == "idle" and c["message"] == "cancelled"


def test_build_daemon_registers_reauth():
    from server.ipc.serve import build_daemon
    app, _orch = build_daemon()
    assert "reauth_start" in app.state.rpc
    assert "reauth_status" in app.state.rpc
    assert "reauth_cancel" in app.state.rpc
    assert app.state.reauth is not None


# ---------------------------------------------------------------------------
# B2c: reauth_submit_code RPC + mode forwarding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reauth_submit_code_rpc_registered():
    """reauth_submit_code RPC must be registered after register_reauth()."""
    app = create_daemon_app()
    fake = _FakeReauth()
    register_reauth(app, fake)
    assert "reauth_submit_code" in app.state.rpc


@pytest.mark.asyncio
async def test_reauth_submit_code_rpc_calls_manager():
    """reauth_submit_code RPC calls reauth.submit_code(name, code) and returns state dict."""
    app = create_daemon_app()
    fake = _FakeReauth()
    register_reauth(app, fake)
    submit = app.state.rpc["reauth_submit_code"]

    result = await submit({"name": "primary", "code": "OAUTH123"})
    assert result["state"] == "running"
    assert ("submit_code", "primary", "OAUTH123") in fake.calls


@pytest.mark.asyncio
async def test_reauth_start_rpc_forwards_mode():
    """reauth_start RPC forwards mode param to reauth.start."""
    app = create_daemon_app()
    fake = _FakeReauth()
    register_reauth(app, fake)
    start = app.state.rpc["reauth_start"]

    await start({"name": "primary", "identity": "me@example.com", "mode": "setup_token"})
    assert ("start", "primary", "me@example.com", "setup_token") in fake.calls


@pytest.mark.asyncio
async def test_reauth_start_rpc_defaults_mode_login():
    """reauth_start RPC defaults mode to 'login' when not provided."""
    app = create_daemon_app()
    fake = _FakeReauth()
    register_reauth(app, fake)
    start = app.state.rpc["reauth_start"]

    await start({"name": "primary", "identity": "me@example.com"})
    assert ("start", "primary", "me@example.com", "login") in fake.calls
