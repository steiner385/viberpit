# server/tests/test_app_x11_lifespan.py
import os
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import create_app
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.x11manager import build_x11_manager
from server.tests.fakes.fake_factory import fake_sdk_factory

_IDENT = {"Tailscale-User-Login": "x11life@example.com"}


class S:  # minimal settings stand-in
    enable_xvfb = False
    display_start = 100
    display_count = 4
    max_browsers = 2
    vnc_port_base = 6100
    xvfb_resolution = "800x600x24"
    vnc_idle_timeout_s = 1800.0


def test_disabled_returns_none():
    assert build_x11_manager(S()) is None


def test_enabled_but_missing_binaries_returns_none(monkeypatch):
    s = S()
    s.enable_xvfb = True
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert build_x11_manager(s) is None  # fail-loud preflight → disabled


class _FakeX11:
    def __init__(self):
        self.sweep_stale_calls = 0
        self.sweep_orphan_calls = 0
    def sweep_stale(self): self.sweep_stale_calls += 1; return 0
    def sweep_orphan_processes(self): self.sweep_orphan_calls += 1; return 0
    async def stop_all(self): pass


def test_edge_startup_reaps_orphan_display_processes(tmp_path):
    """Issue #206 (startup half): the edge sweep only unlinked stale files; it must now
    also reap orphaned Xvfb/x11vnc/chrome processes left by a prior SIGKILLed run."""
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    app = create_app(orchestrator=orch)
    fake = _FakeX11()
    app.state.x11 = fake  # set synchronously in create_app; safe to override pre-lifespan
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url}), \
         TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/api/health", headers=_IDENT).status_code == 200
    assert fake.sweep_orphan_calls == 1
    assert fake.sweep_stale_calls == 1
