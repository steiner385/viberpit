import os
from unittest.mock import patch
from server.tests.fakes.fake_factory import fake_sdk_factory


def _client(tmp_path):
    from server.sessions.orchestrator import SessionOrchestrator
    from server.app import create_app
    from fastapi.testclient import TestClient
    with patch.dict(os.environ, {"VIBERPIT_DB_URL": "sqlite+aiosqlite:///:memory:", "VIBERPIT_HOME": str(tmp_path)}):
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


def test_healthz_reports_busy_and_active_sessions(tmp_path):
    """/healthz surfaces orchestrator drain-readiness for the deployer's idle gate."""
    from server.sessions.engine import AgentEngine
    from server.sessions.types import SessionRecord
    client = _client(tmp_path)
    orch = client.app.state.orchestrator
    orch._records["s1"] = SessionRecord(
        id="s1", name="s1", account="acct", engine=AgentEngine.CLAUDE_CODE, engine_session_id="cs",
        cols=80, rows=24, cwd="/tmp", status="in_progress",
    )
    with client:
        r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["busy"] is True
    assert body["activeSessions"] == 1


def _healthz_with(tmp_path, monkeypatch, caplog, exc):
    client = _client(tmp_path)
    with client:
        def boom(*args, **kwargs):
            raise exc
        monkeypatch.setattr("server.db.db_session", boom)
        with caplog.at_level("DEBUG", logger="server"):
            r = client.get("/healthz")
    return r, [x for x in caplog.records if x.levelname == "WARNING"
               and x.name == "server"]


def test_healthz_transient_db_blip_is_500_but_not_a_warning(tmp_path, monkeypatch, caplog):
    """A connect-level failure still fails the probe but must not file an auto-error (#2790)."""
    r, warnings = _healthz_with(tmp_path, monkeypatch, caplog, ConnectionRefusedError("down"))
    assert r.status_code == 500
    assert r.json()["status"] == "schema_error"
    assert not warnings


def test_healthz_non_transient_db_error_still_warns(tmp_path, monkeypatch, caplog):
    from sqlalchemy.exc import ProgrammingError
    r, warnings = _healthz_with(
        tmp_path, monkeypatch, caplog, ProgrammingError("SELECT", {}, Exception("no column")))
    assert r.status_code == 500
    assert warnings
