import logging

from fastapi.testclient import TestClient

from server.app import create_app

IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}


def _client(monkeypatch):
    monkeypatch.setenv("CCSWITCH_DEV", "1")
    app = create_app()

    @app.get("/api/_explode")
    async def _explode():
        raise RuntimeError("unhandled-boom")

    return TestClient(app, raise_server_exceptions=False)


def test_unhandled_exception_returns_500_and_logs(monkeypatch, caplog):
    client = _client(monkeypatch)
    with caplog.at_level(logging.ERROR):
        r = client.get("/api/_explode", headers=IDENTITY_HEADERS)
    assert r.status_code == 500
    assert r.json()["detail"]  # generic message, not the traceback
    assert any("unhandled-boom" in (rec.exc_text or "") or rec.exc_info
               for rec in caplog.records if rec.levelno >= logging.ERROR)
