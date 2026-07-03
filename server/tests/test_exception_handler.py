import logging

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from server.app import create_app

IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}


def _client(monkeypatch):
    monkeypatch.setenv("CCSWITCH_DEV", "1")
    app = create_app()

    async def _explode():
        raise RuntimeError("unhandled-boom")

    # create_app() mounts the SPA catch-all ("/") LAST, so a route registered here
    # (after create_app returns) sits behind it: on a box with a built web/dist the
    # mount serves index.html (200) for /api/_explode instead of reaching the
    # handler — which is why this passed in CI (no web/dist, no mount) but not
    # locally. Prepend the throwing route so it always matches before the mount.
    app.router.routes.insert(0, APIRoute("/api/_explode", _explode, methods=["GET"]))

    return TestClient(app, raise_server_exceptions=False)


def test_unhandled_exception_returns_500_and_logs(monkeypatch, caplog):
    client = _client(monkeypatch)
    with caplog.at_level(logging.ERROR):
        r = client.get("/api/_explode", headers=IDENTITY_HEADERS)
    assert r.status_code == 500
    assert r.json()["detail"]  # generic message, not the traceback
    assert any("unhandled-boom" in (rec.exc_text or "") or rec.exc_info
               for rec in caplog.records if rec.levelno >= logging.ERROR)
