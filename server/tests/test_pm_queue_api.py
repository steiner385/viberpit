"""PM global priority queue API tests — GET /api/pm/queue.

Mirrors test_pm_deps_api.py fixture pattern: _client() context manager, async ORM
seeding via db_session(), CCSWITCH_DEV=1 + empty allowlist auth bypass.
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


def _seed_ready_and_blocked(c) -> None:
    """One onboarded product with a linked board: a Ready item and a Blocked-by-dep
    item (an active native dep whose destination issue is open).

    Seeded through the app's OWN event loop (same idiom as
    test_cicd_findings_api._seed). A bare ``asyncio.run(_seed_async())`` runs on a
    throwaway loop while the app holds the module-level ``server.db._engine`` on the
    TestClient portal loop. Both share one AsyncAdaptedQueuePool, and that pool's
    ``asyncio.Queue`` binds to whichever loop first *awaits* a checkout —
    ``get_nowait()`` never checks loop affinity, which is why an idle pool tolerates
    the cross-loop seed. Once the orchestrator's control loops saturate the
    5-connection pool at startup, the foreign-loop checkout has to await and dies
    with "Queue ... is bound to a different event loop".
    """
    from server.db import db_session
    from server.db.models import (
        GhProject, GhProjectItem, Project, ProjectRepo, WorkItem, WorkItemDep)

    async def _seed_async() -> None:
        async with db_session() as db:
            proj = Project(slug="ccswitch", name="ccswitch")
            db.add(proj)
            await db.flush()

            db.add(ProjectRepo(project_id=proj.id,
                               repo="steiner385/cc-account-switcher",
                               is_primary=True))

            wi_ready = WorkItem(repo="steiner385/cc-account-switcher", number=100,
                                kind="issue", title="ready item", state="open",
                                body="do the thing", url="https://example.com/100")
            wi_blocked = WorkItem(repo="steiner385/cc-account-switcher", number=101,
                                  kind="issue", title="blocked item", state="open",
                                  body="blocked thing", url="https://example.com/101")
            wi_blocker = WorkItem(repo="steiner385/cc-account-switcher", number=102,
                                  kind="issue", title="blocker item", state="open")
            db.add_all([wi_ready, wi_blocked, wi_blocker])
            await db.flush()

            ghp = GhProject(project_id=proj.id, gh_project_number=7,
                            title="cc-account-switcher")
            db.add(ghp)
            await db.flush()

            db.add(GhProjectItem(gh_project_id=ghp.id, work_item_id=wi_ready.id,
                                 status="Ready", score=5.0))
            db.add(GhProjectItem(gh_project_id=ghp.id, work_item_id=wi_blocked.id,
                                 status="Ready", score=9.0))

            # Active native dep: blocked -> blocker (blocker is open) => blocked-by-dep.
            db.add(WorkItemDep(
                src_ref="steiner385/cc-account-switcher#101",
                dst_ref="steiner385/cc-account-switcher#102",
                source="native",
                active=True,
            ))
            await db.commit()

    assert c.portal is not None
    c.portal.call(_seed_async)


def test_queue_endpoint_ranks_ready_and_skips_blocked():
    with _client() as c:
        _seed_ready_and_blocked(c)

        resp = c.get("/api/pm/queue", headers=IDENTITY_HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert [e["ref"] for e in body["entries"]] == ["steiner385/cc-account-switcher#100"]
        entry = body["entries"][0]
        assert entry["repo"] == "steiner385/cc-account-switcher"
        assert entry["number"] == 100
        assert entry["product"] == "ccswitch"
        assert entry["title"] == "ready item"
        assert entry["url"] == "https://example.com/100"
        assert entry["score"] == 5.0
        assert "body" not in entry

        assert body["skipped"] == {"blocked": 1}
        assert body["generatedAt"]


def test_queue_endpoint_invalid_min_status_422():
    with _client() as c:
        resp = c.get("/api/pm/queue?min_status=Nope", headers=IDENTITY_HEADERS)
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["error"] == "invalid_min_status"


def test_queue_endpoint_requires_identity():
    with _client() as c:
        _seed_ready_and_blocked(c)
        assert c.get("/api/pm/queue").status_code == 401
        assert c.get("/api/pm/queue", headers=IDENTITY_HEADERS).status_code == 200
