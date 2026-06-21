import base64, os
from unittest.mock import patch
from server.tests.fakes.fake_factory import fake_sdk_factory

IDENT = {"Tailscale-User-Login": "testuser@example.com"}
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

def _client(tmp_path):
    from server.sessions.orchestrator import SessionOrchestrator
    from server.app import create_app
    from fastapi.testclient import TestClient
    os.environ["CCSWITCH_HOME"] = str(tmp_path)
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:"}):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
        app = create_app(orchestrator=orch)
        return TestClient(app, raise_server_exceptions=False)

def test_upload_then_serve(tmp_path):
    from server.sessions import uploads
    client = _client(tmp_path)
    with client:
        r = client.post("/api/uploads", headers=IDENT,
                        files={"file": ("shot.png", PNG, "image/png")})
        assert r.status_code == 200
        sha = r.json()["sha"]
        assert r.json()["mime"] == "image/png"
        # associate so the serve route can find it
        uploads.associate("sessX", sha, "image/png")
        g = client.get(f"/api/sessions/sessX/images/{sha}", headers=IDENT)
        assert g.status_code == 200 and g.content == PNG

def test_upload_rejects_non_image(tmp_path):
    client = _client(tmp_path)
    with client:
        r = client.post("/api/uploads", headers=IDENT,
                        files={"file": ("x.png", b"nope", "image/png")})
        assert r.status_code == 400

def test_serve_unknown_404(tmp_path):
    client = _client(tmp_path)
    with client:
        g = client.get("/api/sessions/sessX/images/deadbeef", headers=IDENT)
        assert g.status_code == 404
