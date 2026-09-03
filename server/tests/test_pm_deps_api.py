"""PM deps/drift API tests — GET /api/pm/projects/{slug}/deps.

Mirrors test_pm_api.py fixture pattern: _client() context manager, async ORM seeding
via db_session(), CCSWITCH_DEV=1 + empty allowlist auth bypass.
"""
from __future__ import annotations

import asyncio
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


def test_deps_endpoint_returns_edges_and_drift(tmp_path):  # noqa: ARG001
    with _client() as c:
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

                wi945 = WorkItem(repo="steiner385/cc-account-switcher", number=945,
                                 kind="issue", title="i945", state="open")
                wi949 = WorkItem(repo="steiner385/cc-account-switcher", number=949,
                                 kind="issue", title="i949", state="open")
                wi950 = WorkItem(repo="steiner385/cc-account-switcher", number=950,
                                 kind="issue", title="i950", state="open")
                wi960 = WorkItem(repo="steiner385/cc-account-switcher", number=960,
                                 kind="issue", title="i960", state="open")
                db.add_all([wi945, wi949, wi950, wi960])
                await db.flush()

                ghp = GhProject(project_id=proj.id, gh_project_number=7,
                                title="cc-account-switcher")
                db.add(ghp)
                await db.flush()

                # Only 950 on the board — 960 is open but NOT on the board
                db.add(GhProjectItem(gh_project_id=ghp.id, work_item_id=wi950.id))

                # native dep: 950 → KinDash#12 (dst unmirrored → unresolvedTargets)
                db.add(WorkItemDep(
                    src_ref="steiner385/cc-account-switcher#950",
                    dst_ref="steiner385/KinDash#12",
                    source="native",
                    active=True,
                ))
                # prose dep: 950 → 949 (prose-only, no native counterpart)
                db.add(WorkItemDep(
                    src_ref="steiner385/cc-account-switcher#950",
                    dst_ref="steiner385/cc-account-switcher#949",
                    source="prose",
                    active=True,
                ))
                await db.commit()

        asyncio.run(_seed_async())

        resp = c.get("/api/pm/projects/ccswitch/deps", headers=IDENTITY_HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["project"]["number"] == 7
        srcs = {e["src"] for e in body["edges"]}
        assert "steiner385/cc-account-switcher#950" in srcs

        assert body["drift"]["proseOnly"] == [
            {"src": "steiner385/cc-account-switcher#950",
             "dst": "steiner385/cc-account-switcher#949"}]
        assert any(e["dst"] == "steiner385/KinDash#12"
                   for e in body["drift"]["unresolvedTargets"])
        assert "steiner385/cc-account-switcher#960" in body["drift"]["missingFromProject"]


def test_deps_endpoint_404_on_unknown_slug():
    with _client() as c:
        assert c.get(
            "/api/pm/projects/nope/deps", headers=IDENTITY_HEADERS
        ).status_code == 404


def test_deps_endpoint_null_project_when_no_gh_project():
    """When no GhProject is linked, project is null and missingFromProject is []."""
    with _client() as c:
        from server.db import db_session
        from server.db.models import Project, ProjectRepo, WorkItem, WorkItemDep

        async def _seed_async() -> None:
            async with db_session() as db:
                proj = Project(slug="ccswitch", name="ccswitch")
                db.add(proj)
                await db.flush()
                db.add(ProjectRepo(project_id=proj.id,
                                   repo="steiner385/cc-account-switcher",
                                   is_primary=True))
                wi950 = WorkItem(repo="steiner385/cc-account-switcher", number=950,
                                 kind="issue", title="i950", state="open")
                db.add(wi950)
                await db.flush()
                db.add(WorkItemDep(
                    src_ref="steiner385/cc-account-switcher#950",
                    dst_ref="steiner385/cc-account-switcher#949",
                    source="prose",
                    active=True,
                ))
                await db.commit()

        asyncio.run(_seed_async())

        resp = c.get("/api/pm/projects/ccswitch/deps", headers=IDENTITY_HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["project"] is None
        assert body["drift"]["missingFromProject"] == []
