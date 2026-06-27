import pytest
from server.ipc.daemon import create_daemon_app
from server.ipc.reauth_rpc import register_reauth
from server.reauth import ReauthState


class _FakeReauth:
    def __init__(self):
        self.calls = []
    async def start(self, name, identity):
        self.calls.append(("start", name, identity))
        return ReauthState(state="running", identity=identity, vnc_session="__reauth__")
    def status(self, name):
        return ReauthState(state="running", vnc_session="__reauth__")
    async def cancel(self, name):
        self.calls.append(("cancel", name))
        return ReauthState(state="idle", message="cancelled")


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
    assert ("start", "primary", "me@example.com") in fake.calls

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
