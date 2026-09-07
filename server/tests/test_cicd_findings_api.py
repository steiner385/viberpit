"""GET /api/automations/cicd-doctor/findings — filters + shape."""
from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

H = {"Tailscale-User-Login": "testuser@example.com"}


@contextmanager
def _client():
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url,
                                     "CCSWITCH_DEV": "1",
                                     "CCSWITCH_TAILNET_ALLOWLIST": ""}):
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            app = create_app(orchestrator=orch)
            with TestClient(app, raise_server_exceptions=False) as c:
                yield c


async def _seed_rows():
    from server.db import db_session
    from server.db.models import CiFinding
    async with db_session() as db:
        db.add(CiFinding(repo="o/r", kind="flaky_test", fingerprint="f1",
                         status="open", title="flaky", detail={"flake_pct": 8.0}))
        db.add(CiFinding(repo="o/r", kind="waste_double_run", fingerprint="f2",
                         status="resolved", title="waste", detail={}))
        await db.flush()


def _seed(c):
    """Seed through the app's OWN event loop (same idiom as
    test_assistant_external_token._mint_and_insert).

    A bare ``asyncio.run(_seed_rows())`` runs on a throwaway loop while the app
    holds the module-level ``server.db._engine`` on the TestClient portal loop.
    Both share one AsyncAdaptedQueuePool, and that pool's ``asyncio.Queue`` binds
    to whichever loop first *awaits* a checkout — ``get_nowait()`` never checks
    loop affinity, which is why an idle pool tolerates the cross-loop seed. Once
    the orchestrator's control loops saturate the 5-connection pool at startup,
    the foreign-loop checkout has to await and dies with
    "Queue ... is bound to a different event loop".
    """
    assert c.portal is not None
    c.portal.call(_seed_rows)


def test_findings_default_open_only_and_shape():
    with _client() as c:
        _seed(c)
        body = c.get("/api/automations/cicd-doctor/findings", headers=H).json()
        assert [f["kind"] for f in body] == ["flaky_test"]
        f = body[0]
        assert f["status"] == "open" and f["issueNumber"] is None
        assert f["detail"] == {"flake_pct": 8.0} and f["firstSeenAt"]


def test_findings_status_and_repo_filters():
    with _client() as c:
        _seed(c)
        assert len(c.get("/api/automations/cicd-doctor/findings?status=all",
                         headers=H).json()) == 2
        assert c.get("/api/automations/cicd-doctor/findings?repo=o/other",
                     headers=H).json() == []


def test_findings_requires_auth():
    with _client() as c:
        assert c.get("/api/automations/cicd-doctor/findings").status_code == 401
