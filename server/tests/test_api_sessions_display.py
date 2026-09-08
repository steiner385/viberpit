"""POST /api/sessions carries client_node + display to the orchestrator (spec §4.8)."""
from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import create_app
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory
from server.tests.test_api import IDENTITY_HEADERS
from server.tests.test_session_display_mode_persistence import _rec


def _make_app():
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
    return create_app(orchestrator=orch)


def test_create_forwards_client_node_and_display(isolated_db):
    app = _make_app()
    seen: dict = {}

    async def fake_create(**kw):
        seen.update(kw)
        return _rec(id="s-new", display_mode="native")

    app.state.sessions.create_session = fake_create
    with patch("server.auth.middleware.client_node_name", lambda req: "dobby"):
        client = TestClient(app, raise_server_exceptions=True)
        with client:
            r = client.post("/api/sessions", json={"display": "auto"},
                             headers=IDENTITY_HEADERS)
    assert r.status_code == 201, r.text
    assert seen["client_node"] == "dobby"
    assert seen["display"] == "auto"
    assert r.json()["displayMode"] == "native"


def test_create_rejects_unknown_display_value(isolated_db):
    app = _make_app()
    client = TestClient(app, raise_server_exceptions=True)
    with client:
        r = client.post("/api/sessions", json={"display": "desktop"},
                         headers=IDENTITY_HEADERS)
    assert r.status_code == 422
