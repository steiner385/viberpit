"""The Origin allowlist (server/auth/origin.py): cross-site WebSocket hijacking and
cross-origin writes (D-18 spike §E.2).

Identity is the Tailscale header on every request, so before this check any page open
in a tailnet browser could open /ws and send prompts as the owner.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocketDisconnect

from server.auth import origin as og

PUBLIC = "https://dobby.tail0dd570.ts.net"
_ID = {"Tailscale-User-Login": "origintest@example.com"}
_ENV = {"VIBERPIT_DEV": "1", "VIBERPIT_PUBLIC_ORIGIN": PUBLIC, "VIBERPIT_ALLOWED_ORIGINS": ""}
EDGE = "http://127.0.0.1:8787"


# --- the rule ---------------------------------------------------------------------------

@pytest.mark.parametrize("origin,hosts,ok", [
    # The configured public origin (nginx :443 sends Host=127.0.0.1:8787 upstream).
    (PUBLIC, {"127.0.0.1:8787"}, True),
    (PUBLIC + ":443", {"127.0.0.1:8787"}, True),
    # tailscale serve on :8443 keeps the browser's Host.
    (PUBLIC + ":8443", {"dobby.tail0dd570.ts.net:8443"}, True),
    (PUBLIC + ":8443", {"127.0.0.1:8787", "dobby.tail0dd570.ts.net:8443"}, True),
    # Another port on the public host (a preview origin) is a different origin.
    (PUBLIC + ":8444", {"dobby.tail0dd570.ts.net:8443"}, False),
    # Direct loopback.
    (EDGE, {"127.0.0.1:8787"}, True),
    ("http://localhost:8787", {"localhost:8787"}, True),
    # Hostile pages.
    ("https://evil.example", {"127.0.0.1:8787"}, False),
    ("https://evil.example", {"evil.example"}, False),       # DNS rebinding: untrusted name
    ("null", {"127.0.0.1:8787"}, False),
    ("http://dobby.tail0dd570.ts.net", {"127.0.0.1:8787"}, False),  # wrong scheme
    ("", {"127.0.0.1:8787"}, False),
])
def test_origin_rule(origin, hosts, ok):
    with patch.dict(os.environ, {**_ENV, "VIBERPIT_DEV": "0"}):
        assert og.origin_allowed(origin, hosts) is ok


def test_extra_origins_and_dev_origins():
    with patch.dict(os.environ, {**_ENV, "VIBERPIT_DEV": "0",
                                 "VIBERPIT_ALLOWED_ORIGINS": "http://dobby.local:8787, junk"}):
        assert og.origin_allowed("http://dobby.local:8787", set())
        assert not og.origin_allowed("http://localhost:5173", set())
    with patch.dict(os.environ, {**_ENV, "VIBERPIT_DEV": "1"}):
        assert og.origin_allowed("http://localhost:5173", set())
        assert og.origin_allowed("http://127.0.0.1:4173", set())


# --- the real app -----------------------------------------------------------------------

def _client():
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory
    from server.app import create_app

    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
        app = create_app(orchestrator=orch)
    return TestClient(app, base_url=EDGE, raise_server_exceptions=False)


def _ws_paths(routes, prefix: str = "") -> list[str]:
    """Every WebSocket route path, walking FastAPI ≥0.141's lazy ``_IncludedRouter``
    wrappers (``app.routes`` is no longer flat), so a new socket cannot hide in one."""
    out: list[str] = []
    for r in routes:
        inner = getattr(r, "original_router", None)
        if inner is not None:
            ctx = getattr(r, "include_context", None)
            out += _ws_paths(inner.routes, prefix + (getattr(ctx, "prefix", "") or ""))
        elif isinstance(r, WebSocketRoute):
            out.append(prefix + r.path)
    return out


def test_every_websocket_route_refuses_a_cross_origin_upgrade():
    client = _client()
    paths = _ws_paths(client.app.routes)
    # Not vacuous: the three browser sockets are all enumerated.
    assert {"/ws", "/ws/vnc", "/ws/stt"} <= set(paths), paths
    with patch.dict(os.environ, _ENV):
        for path in paths:
            url = path.replace("{", "").replace("}", "") + "?session=S"
            with pytest.raises(WebSocketDisconnect) as ei:
                with client.websocket_connect(
                        url, headers={**_ID, "Origin": "https://evil.example"}):
                    pass
            assert ei.value.code == og.WS_CLOSE_POLICY_VIOLATION, path
            # The guard's own reason: proves IT refused, not the route's identity check.
            assert ei.value.reason == og.REFUSAL_REASON, path


@pytest.mark.parametrize("origin", [EDGE, PUBLIC, None])
def test_same_origin_and_native_upgrades_pass_the_guard(origin):
    """Same-origin and no-Origin (CLI, ``viberpit ask``) upgrades reach the route: /ws/vnc
    with Xvfb off answers its own 4401, not the guard's 1008."""
    client = _client()
    client.app.state.x11 = None
    headers = dict(_ID) if origin is None else {**_ID, "Origin": origin}
    with patch.dict(os.environ, _ENV):
        # A full URL: the WS test client joins a bare path onto ws://testserver.
        with client.websocket_connect("ws://127.0.0.1:8787/ws/vnc?session=S",
                                      headers=headers) as ws:
            with pytest.raises(WebSocketDisconnect) as ei:
                ws.receive_bytes()
    assert ei.value.code == 4401


_WRITE = "/api/sessions/S/browser/open"


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_cross_origin_write_is_refused(method):
    client = _client()
    with patch.dict(os.environ, _ENV):
        r = client.request(method.upper(), _WRITE,
                           headers={**_ID, "Origin": "https://evil.example"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == og.REFUSAL_CODE


def test_cross_site_write_without_origin_is_refused():
    client = _client()
    with patch.dict(os.environ, _ENV):
        r = client.post(_WRITE, json={}, headers={**_ID, "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


@pytest.mark.parametrize("headers", [
    {"Origin": EDGE},
    {"Origin": PUBLIC},
    {},                                             # native client: no Origin at all
    {"Sec-Fetch-Site": "same-origin"},
])
def test_same_origin_and_native_writes_pass_the_guard(headers):
    """They reach the route (409: no display), not the guard's 403."""
    client = _client()
    with patch.dict(os.environ, _ENV):
        r = client.post(_WRITE, json={}, headers={**_ID, **headers})
    assert r.status_code == 409, r.text


def test_reads_are_not_checked():
    client = _client()
    with patch.dict(os.environ, _ENV):
        r = client.get("/api/x11/metrics", headers={**_ID, "Origin": "https://evil.example"})
    assert r.status_code == 200, r.text
