"""Reauth's VNC stream stays on the master even when a daemon endpoint is set.

Reauth runs on the master because the master owns the credentials (#732), so its
Xvfb display is spawned locally. The /ws/vnc route, however, proxied *every*
session to the daemon whenever one was configured, so the browser attached to a
worker that had never spawned the display and the viewer sat at "Reconnecting…".
"""
import pytest

from server.bridge import vnc_ws
from server.bridge.vnc_ws import build_vnc_ws_router
from server.reauth import REAUTH_DISPLAY_KEY


class _FakeX11:
    def __init__(self):
        self.attached = []
        self.detached = []

    def display_for(self, sid):
        return 100

    async def attach(self, sid):
        self.attached.append(sid)
        return 6100

    def detach(self, sid):
        self.detached.append(sid)


class _FakeWS:
    def __init__(self, session):
        self.query_params = {"session": session}
        self.accepted = False
        self.closed = None

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)


class _Boom(Exception):
    """Raised to stop each route just past the point that identifies it."""


def _route(router):
    for r in router.routes:
        if getattr(r, "path", None) == "/ws/vnc":
            return r.endpoint
    raise AssertionError("/ws/vnc not mounted")


@pytest.fixture()
def harness(monkeypatch):
    """Remote mode: a daemon endpoint is configured and every path is instrumented."""
    local, remote, proxied = _FakeX11(), _FakeX11(), []

    def _fake_proxy(endpoint, path, ssl):
        proxied.append(path)
        raise _Boom("proxied to daemon")

    async def _fake_rfb(port, **kw):
        raise _Boom("local relay reached")

    monkeypatch.setattr(vnc_ws, "daemon_ws_connect", _fake_proxy)
    monkeypatch.setattr(vnc_ws, "_open_rfb_connection", _fake_rfb)

    router = build_vnc_ws_router(
        lambda: remote,
        lambda: "unix:///tmp/nonexistent.sock",   # remote mode is ON
        lambda: None,
        lambda sid: local if sid == REAUTH_DISPLAY_KEY else None,
    )
    return _route(router), local, remote, proxied


@pytest.mark.asyncio
async def test_reauth_session_relays_locally_not_to_daemon(harness):
    route, local, remote, proxied = harness
    ws = _FakeWS(REAUTH_DISPLAY_KEY)
    await route(websocket=ws, identity="pin@example.com")

    assert not proxied, f"reauth must not proxy to the daemon, but did: {proxied}"
    assert local.attached == [REAUTH_DISPLAY_KEY], (
        f"reauth must attach the LOCAL display on the master; attaches={local.attached}"
    )
    assert local.detached == [REAUTH_DISPLAY_KEY], "the local attach must be released"
    assert remote.attached == [], "the daemon's x11 must not be touched for reauth"


@pytest.mark.asyncio
async def test_ordinary_session_still_proxies_to_daemon(harness):
    route, local, remote, proxied = harness
    ws = _FakeWS("S1")
    await route(websocket=ws, identity="pin@example.com")

    assert proxied == ["/vnc?session=S1"], (
        f"non-reauth sessions must still proxy to the daemon; proxied={proxied}"
    )
    assert local.attached == [], "an ordinary session must not touch the local display"


@pytest.mark.asyncio
async def test_reauth_falls_back_to_daemon_when_no_local_display(monkeypatch):
    """With no local reauth display, the resolver returns None and routing is unchanged."""
    proxied = []

    def _fake_proxy(endpoint, path, ssl):
        proxied.append(path)
        raise _Boom("proxied to daemon")

    monkeypatch.setattr(vnc_ws, "daemon_ws_connect", _fake_proxy)
    router = build_vnc_ws_router(
        lambda: _FakeX11(),
        lambda: "unix:///tmp/nonexistent.sock",
        lambda: None,
        lambda sid: None,
    )
    await _route(router)(websocket=_FakeWS(REAUTH_DISPLAY_KEY), identity="pin@example.com")
    assert proxied == [f"/vnc?session={REAUTH_DISPLAY_KEY}"]
