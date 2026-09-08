"""Tests for POST /api/sessions/{id}/browser/open managed-browser action."""
import os
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import create_app

_IDENTITY_HEADERS = {"Tailscale-User-Login": "browsertest@example.com"}

_ENV_PATCH = {
    "CCSWITCH_DEV": "1",
}


class FakeX11:
    def __init__(self, ok, has_display=True):
        self._ok = ok
        self._has_display = has_display
        self.calls = []

    def display_for(self, sid):
        return object() if self._has_display else None

    async def launch_browser(self, sid, url):
        self.calls.append((sid, url))
        return self._ok


def _make_client(fake_x11):
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    with patch.dict(os.environ, _ENV_PATCH):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
        app = create_app(orchestrator=orch)
        # The route delegates to orchestrator.open_browser(), which reads the
        # orchestrator's OWN _x11 (that is where the native/virtual branch lives —
        # one implementation shared with the daemon's x11_open_browser RPC). Injecting
        # only app.state.x11 would leave the orchestrator with x11=None, so every
        # request would 409 "no display for session". app.state.x11 stays set because
        # other routes still read it.
        orch._x11 = fake_x11
        app.state.x11 = fake_x11
        return TestClient(app, raise_server_exceptions=False)


def test_open_browser_ok():
    fake = FakeX11(ok=True)
    client = _make_client(fake)
    r = client.post(
        "/api/sessions/S/browser/open",
        json={"url": "https://example.com"},
        headers=_IDENTITY_HEADERS,
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert fake.calls == [("S", "https://example.com")]


def test_open_browser_capped():
    fake = FakeX11(ok=False)
    client = _make_client(fake)
    r = client.post(
        "/api/sessions/S/browser/open",
        json={},
        headers=_IDENTITY_HEADERS,
    )
    assert r.status_code == 409
    # reason "cap" — the display exists, launch_browser refused.
    assert r.json()["detail"] == "browser cap reached"
    assert fake.calls == [("S", None)]


def test_open_browser_no_display():
    fake = FakeX11(ok=True, has_display=False)
    client = _make_client(fake)
    r = client.post(
        "/api/sessions/S/browser/open",
        json={},
        headers=_IDENTITY_HEADERS,
    )
    assert r.status_code == 409
    # reason "no_display" — no lease, so launch_browser is never reached.
    assert r.json()["detail"] == "no display for session"
    assert fake.calls == []


def test_open_browser_no_x11():
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    with patch.dict(os.environ, _ENV_PATCH):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
        app = create_app(orchestrator=orch)
        # Neither the orchestrator nor app.state has an X11 manager.
        assert orch._x11 is None
        app.state.x11 = None
        client = TestClient(app, raise_server_exceptions=False)
    r = client.post(
        "/api/sessions/S/browser/open",
        json={},
        headers=_IDENTITY_HEADERS,
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "no display for session"
