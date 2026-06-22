# server/tests/test_healthz_vnc.py
"""
Verify that /healthz includes "vncAttached" and that the value reflects
app.state.x11 — false when x11 is None, true when x11.metrics() reports
a connected VNC client.

/healthz is auth-exempt so no Tailscale identity header is needed.
"""
import os
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient

from server.tests.fakes.fake_factory import fake_sdk_factory


def _client(tmp_path):
    from server.sessions.orchestrator import SessionOrchestrator
    from server.app import create_app

    env = {
        "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
        "CCSWITCH_HOME": str(tmp_path),
        "CCSWITCH_DEV": "1",
    }
    with patch.dict(os.environ, env):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
        app = create_app(orchestrator=orch)
    return app


def test_healthz_vnc_attached_false_when_x11_none(tmp_path):
    """/healthz vncAttached is false when app.state.x11 is None (feature disabled)."""
    app = _client(tmp_path)
    app.state.x11 = None
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert "vncAttached" in body
    assert body["vncAttached"] is False


def test_healthz_vnc_attached_true_when_client_connected(tmp_path):
    """/healthz vncAttached is true when x11.metrics() reports vnc_clients_attached >= 1."""
    app = _client(tmp_path)
    fake_x11 = MagicMock()
    fake_x11.metrics.return_value = {"vnc_clients_attached": 1}
    app.state.x11 = fake_x11
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert "vncAttached" in body
    assert body["vncAttached"] is True


def test_healthz_vnc_attached_false_when_no_clients(tmp_path):
    """/healthz vncAttached is false when x11.metrics() reports vnc_clients_attached == 0."""
    app = _client(tmp_path)
    fake_x11 = MagicMock()
    fake_x11.metrics.return_value = {"vnc_clients_attached": 0}
    app.state.x11 = fake_x11
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["vncAttached"] is False
