"""PM registry API tests (spec §7, phase-1 routes).

One narrated flow (create → list → sync-now → edges) plus edge cases, in the
style of test_sdlc_api.py — reuse that module's fixtures.
"""
from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}


@contextmanager
def _client():
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    # Unset remote-sessiond vars so create_app() stays in-process (no TLS needed).
    _clear = [
        "CCSWITCH_SESSIOND_ENDPOINT",
        "CCSWITCH_SESSIOND_SOCKET",
        "CCSWITCH_SESSIOND_HOST",
        "CCSWITCH_SESSIOND_PORT",
        "CCSWITCH_SESSIOND_CLIENT_CERT",
        "CCSWITCH_SESSIOND_CLIENT_KEY",
        "CCSWITCH_SESSIOND_CA",
    ]
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        saved = {k: os.environ.pop(k, None) for k in _clear}
        try:
            with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url,
                                         "CCSWITCH_DEV": "1",
                                         "CCSWITCH_TAILNET_ALLOWLIST": ""}):
                orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
                app = create_app(orchestrator=orch)
                with TestClient(app, raise_server_exceptions=False) as c:
                    yield c
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


def test_project_lifecycle():
    """Narrated create → list → sync-now → 404 flow."""
    with _client() as c:
        # create
        resp = c.post("/api/pm/projects", json={
            "name": "KinDash",
            "repos": [{"repo": "steiner385/KinDash", "isPrimary": True}],
        }, headers=IDENTITY_HEADERS)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["slug"] == "kindash"
        assert body["onboardingState"] == "registered"

        # duplicate slug conflicts
        resp = c.post("/api/pm/projects", json={
            "name": "KinDash", "repos": [{"repo": "x/y"}]},
            headers=IDENTITY_HEADERS)
        assert resp.status_code == 409

        # empty repos rejected
        resp = c.post("/api/pm/projects", json={"name": "Norepo", "repos": []},
                      headers=IDENTITY_HEADERS)
        assert resp.status_code == 422

        # list carries repos + counts
        resp = c.get("/api/pm/projects", headers=IDENTITY_HEADERS)
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        proj = items[0]
        assert proj["repos"][0]["repo"] == "steiner385/KinDash"
        assert proj["workItemCount"] == 0
        assert proj["featureCount"] == 0

        # sync-now stamps the request
        resp = c.post("/api/pm/projects/kindash/sync", headers=IDENTITY_HEADERS)
        assert resp.status_code == 202
        assert resp.json() == {"requested": True}

        # detail 404
        resp = c.get("/api/pm/projects/nope", headers=IDENTITY_HEADERS)
        assert resp.status_code == 404


def test_edges():
    """Create two projects, link with an edge, verify bad-kind/bad-dst 422/404, delete."""
    with _client() as c:
        for name in ("Alpha", "Beta"):
            resp = c.post("/api/pm/projects", json={
                "name": name, "repos": [{"repo": f"o/{name.lower()}"}]},
                headers=IDENTITY_HEADERS)
            assert resp.status_code == 201, resp.text

        resp = c.post("/api/pm/projects/alpha/edges", json={
            "dstSlug": "beta", "kind": "depends_on"},
            headers=IDENTITY_HEADERS)
        assert resp.status_code == 201, resp.text
        edge_id = resp.json()["id"]

        # bad kind → 422
        resp = c.post("/api/pm/projects/alpha/edges", json={
            "dstSlug": "beta", "kind": "bogus"},
            headers=IDENTITY_HEADERS)
        assert resp.status_code == 422

        # unknown dst → 404
        resp = c.post("/api/pm/projects/alpha/edges", json={
            "dstSlug": "ghost", "kind": "depends_on"},
            headers=IDENTITY_HEADERS)
        assert resp.status_code == 404

        # delete edge → 204
        resp = c.delete(f"/api/pm/projects/alpha/edges/{edge_id}",
                        headers=IDENTITY_HEADERS)
        assert resp.status_code == 204
