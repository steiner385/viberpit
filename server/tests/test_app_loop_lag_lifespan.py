# server/tests/test_app_loop_lag_lifespan.py
"""The edge process must run the loop-lag watchdog too (P1 final-review wave).

The watchdog was originally wired only into the daemon (server/ipc/serve.py), but
the edge runs the /api/pipeline/events SSE fan-out and, in single-process mode, the
whole orchestrator — so it is exposed to the same RC1 saturation.
"""
import os
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import create_app
from server.obs.loop_lag import LoopLagWatchdog
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

_IDENT = {"Tailscale-User-Login": "looplag@example.com"}


def test_edge_lifespan_starts_and_stops_the_loop_lag_watchdog(tmp_path):
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    app = create_app(orchestrator=orch)
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url}), \
         TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/api/health", headers=_IDENT).status_code == 200
        assert isinstance(app.state.loop_lag, LoopLagWatchdog)
        task = app.state.loop_lag_task
        assert not task.done(), "watchdog task must be running during the app's life"

    # Shutdown must cancel it — a survivor would emit
    # "Task was destroyed but it is pending" at interpreter exit.
    assert task.done()
