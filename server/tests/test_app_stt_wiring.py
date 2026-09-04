"""create_app mounts /ws/stt and only builds an engine when enabled + installed."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server.stt.ws import CLOSE_NOT_ENABLED


def _app(**env):
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory
    base = {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:", "CCSWITCH_DEV": "1",
            "CCSWITCH_TAILNET_ALLOWLIST": ""}
    with patch.dict(os.environ, {**base, **env}):
        os.environ.pop("CCSWITCH_ENABLE_STT", None) if "CCSWITCH_ENABLE_STT" not in env else None
        return create_app(orchestrator=SessionOrchestrator(sdk_factory=fake_sdk_factory()))


def test_route_mounted_and_engine_absent_by_default():
    # FastAPI ≥0.141 mounts include_router() targets lazily behind an internal
    # _IncludedRouter wrapper, so app.routes no longer contains a flat list of
    # path strings to introspect (the brief's original `any(r.path == ... for r
    # in app.routes)` check silently finds nothing and would false-negative).
    # Exercise the route for real instead: a connection that reaches the
    # CLOSE_NOT_ENABLED close code proves both that /ws/stt is mounted AND that
    # no engine was built, in one behavioral assertion.
    app = _app()
    assert app.state.stt_engine is None
    client = TestClient(app, raise_server_exceptions=False)
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect(
            "/ws/stt", headers={"Tailscale-User-Login": "alice@example.com"}
        ) as ws:
            ws.receive_text()
    assert ei.value.code == CLOSE_NOT_ENABLED


def test_engine_built_when_enabled_and_available():
    with patch("server.app.stt_available", return_value=True):
        app = _app(CCSWITCH_ENABLE_STT="1", CCSWITCH_STT_MODEL="tiny.en", CCSWITCH_STT_CPU_THREADS="2")
    eng = app.state.stt_engine
    assert eng is not None and eng.model_name == "tiny.en" and eng.cpu_threads == 2
    assert eng.loaded is False       # create_app never loads; the lifespan warms in a thread


def test_engine_absent_when_enabled_but_not_installed():
    with patch("server.app.stt_available", return_value=False):
        app = _app(CCSWITCH_ENABLE_STT="1")
    assert app.state.stt_engine is None
