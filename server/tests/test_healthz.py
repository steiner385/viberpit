import os
from unittest.mock import patch
from server.tests.fakes.fake_factory import fake_sdk_factory


def _client(tmp_path):
    from server.sessions.orchestrator import SessionOrchestrator
    from server.app import create_app
    from fastapi.testclient import TestClient
    os.environ["CCSWITCH_HOME"] = str(tmp_path)
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
        app = create_app(orchestrator=orch)
        return TestClient(app, raise_server_exceptions=False)


def test_healthz_ok_without_identity(tmp_path):
    """/healthz returns 200 with NO Tailscale identity header (it must be exempt
    from IdentityMiddleware so the deploy can probe it)."""
    client = _client(tmp_path)
    with client:
        r = client.get("/healthz")          # no identity header
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_healthz_500_on_schema_error(tmp_path, monkeypatch):
    """If the DB query fails (e.g. a missing column raises), /healthz reports
    500 schema_error rather than letting the exception escape."""
    client = _client(tmp_path)
    with client:
        def boom(*args, **kwargs):
            raise RuntimeError("no such column: sessions.tool_allowlist")
        # healthz resolves db_session dynamically via the module, so this is seen.
        monkeypatch.setattr("server.db.db_session", boom)
        r = client.get("/healthz")
    assert r.status_code == 500
    assert r.json()["status"] == "schema_error"
