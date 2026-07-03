"""Issue #434: GET /api/daemon/health exposes the DaemonHealthMonitor snapshot.

In-process (dev/CI) has no separate daemon, so the endpoint reports ``available: false``.
When a monitor is present (remote mode) it returns that monitor's snapshot verbatim.
"""
import os
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import create_app
from server.sessions.daemon_health import DaemonHealthMonitor
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

_IDENT = {"Tailscale-User-Login": "health@example.com"}


def _client(tmp_path):
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    app = create_app(orchestrator=orch)
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    ctx = patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url})
    return app, ctx


def test_daemon_health_unavailable_in_process(tmp_path):
    app, ctx = _client(tmp_path)
    with ctx, TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/daemon/health", headers=_IDENT)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["available"] is False
        assert body["restartCount24h"] == 0


def test_daemon_health_requires_identity(tmp_path):
    app, ctx = _client(tmp_path)
    with ctx, TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/daemon/health")
        assert resp.status_code == 401


def test_daemon_health_returns_monitor_snapshot(tmp_path):
    app, ctx = _client(tmp_path)
    with ctx, TestClient(app, raise_server_exceptions=False) as client:
        # Inject a monitor that has observed one restart. Pin the clock so the endpoint's
        # snapshot() (which reads it) sees the synthetic restart inside the 24h window.
        mon = DaemonHealthMonitor(info_fn=None, clock=lambda: 2000.0)
        mon.record("18c0abcd-1-aaaa1111", now=1000.0)
        mon.record("18c0ffff-2-bbbb2222", now=2000.0)
        app.state.daemon_health = mon
        resp = client.get("/api/daemon/health", headers=_IDENT)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["available"] is True
        assert body["restartCount24h"] == 1
        assert body["epoch"] == "18c0ffff-2-bbbb2222"
        assert body["lastRestartAt"] is not None
