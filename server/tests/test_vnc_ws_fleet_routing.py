"""/ws/vnc reaches the daemon of the session's OWN host (D-18 spike §A.2).

Before: the edge always proxied to the primary daemon, so a worker (rocky) session's
Browser tab got 4404 "no display" while its Xvfb ran on the worker. Now the route
resolves through the fleet's per-session routing, like x11_open_browser.
"""
from __future__ import annotations

import pytest

from server.bridge import vnc_ws
from server.ipc.paths import Endpoint
from server.sessions.fleet_orchestrator import FleetOrchestrator
from server.sessions.fleet_router import FleetRouter
from server.sessions.remote import RemoteOrchestrator

PRIMARY_EP = Endpoint("unix", path="/run/primary.sock")
WORKER_EP = Endpoint("https", host="rocky.example", port=9443)


class _Client:
    """Just the DaemonClient surface daemon_route_for_session reads."""

    def __init__(self, endpoint, ssl_context):
        self.endpoint = endpoint
        self.ssl_context = ssl_context


class _BrowserWS:
    def __init__(self, session: str):
        self.query_params = {"session": session}
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = ""):
        pass


def _fleet() -> FleetOrchestrator:
    router = FleetRouter()
    router.add("dobby", RemoteOrchestrator(_Client(PRIMARY_EP, None)))
    router.add("rocky", RemoteOrchestrator(_Client(WORKER_EP, "ROCKY-SSL")))
    fleet = FleetOrchestrator(router, primary="dobby")
    fleet._host_cache["worker-sess"] = "rocky"
    fleet._host_cache["primary-sess"] = "dobby"
    return fleet


@pytest.mark.asyncio
@pytest.mark.parametrize("session,endpoint,ssl_ctx", [
    ("worker-sess", WORKER_EP, "ROCKY-SSL"),
    ("primary-sess", PRIMARY_EP, None),
])
async def test_vnc_socket_proxies_to_the_owning_hosts_daemon(monkeypatch, session, endpoint,
                                                              ssl_ctx):
    dialed: list = []

    class _Closed(Exception):
        pass

    def _fake_connect(ep, path, ssl=None):
        dialed.append((ep, path, ssl))
        raise _Closed()

    monkeypatch.setattr(vnc_ws, "daemon_ws_connect", _fake_connect)
    router = vnc_ws.build_vnc_ws_router(lambda: None, _fleet().daemon_route_for_session)
    ws = _BrowserWS(session)
    await router.routes[0].endpoint(websocket=ws, identity="t@example.com")
    assert dialed == [(endpoint, f"/vnc?session={session}", ssl_ctx)]
    assert ws.accepted


@pytest.mark.asyncio
async def test_route_reads_the_sessions_host_stamp_when_uncached(monkeypatch):
    """A cold cache resolves through the fleet's one host lookup (``_host_for``)."""
    fleet = _fleet()
    fleet._host_cache.clear()

    async def _host_for(sid):
        return {"worker-sess": "rocky"}.get(sid)

    monkeypatch.setattr(fleet, "_host_for", _host_for)
    assert await fleet.daemon_route_for_session("worker-sess") == (WORKER_EP, "ROCKY-SSL")
