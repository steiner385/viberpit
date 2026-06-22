"""Tests for POST /api/sessions/{id}/browser/open managed-browser action."""
import os
from unittest.mock import patch

import pytest
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
    with patch.dict(os.environ, _ENV_PATCH):
        app = create_app()
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


def test_open_browser_no_display():
    fake = FakeX11(ok=True, has_display=False)
    client = _make_client(fake)
    r = client.post(
        "/api/sessions/S/browser/open",
        json={},
        headers=_IDENTITY_HEADERS,
    )
    assert r.status_code == 409


def test_open_browser_no_x11():
    with patch.dict(os.environ, _ENV_PATCH):
        app = create_app()
        app.state.x11 = None
        client = TestClient(app, raise_server_exceptions=False)
    r = client.post(
        "/api/sessions/S/browser/open",
        json={},
        headers=_IDENTITY_HEADERS,
    )
    assert r.status_code == 409
