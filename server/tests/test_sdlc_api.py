"""/api/sdlc router + build handoff correlation (#801, #802)."""
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

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url,
                                     "CCSWITCH_DEV": "1",
                                     "CCSWITCH_TAILNET_ALLOWLIST": ""}):
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            app = create_app(orchestrator=orch)
            with TestClient(app, raise_server_exceptions=False) as c:
                yield c


def test_create_and_list_initiative():
    """POST 201 → slug derived from title; GET list shows stage=intake status=proposed."""
    with _client() as c:
        r = c.post("/api/sdlc/initiatives",
                   json={"title": "Dark mode", "repo": "steiner385/ccswitch"},
                   headers=IDENTITY_HEADERS)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["slug"] == "dark-mode"
        assert body["stage"] == "intake"
        assert body["status"] == "proposed"

        listed = c.get("/api/sdlc/initiatives", headers=IDENTITY_HEADERS)
        assert listed.status_code == 200
        items = listed.json()
        assert len(items) == 1
        assert items[0]["slug"] == "dark-mode"
        assert items[0]["stage"] == "intake"
        assert items[0]["status"] == "proposed"


def test_list_includes_artifact_chain():
    """GET list returns each initiative's committed artifacts inline (#803), so the
    web board renders N stage tracks from one call rather than a detail fetch per row."""
    from server.db import db_session
    from server.db.models import Initiative, InitiativeArtifact

    async def _seed(db_url: str):
        from server.db import init_db
        await init_db(db_url)
        async with db_session() as db:
            ini = Initiative(
                slug="dark-mode",
                repo="steiner385/ccswitch",
                title="Dark mode",
                stage="design",
                status="active",
            )
            db.add(ini)
            await db.flush()
            db.add(InitiativeArtifact(
                initiative_id=ini.id,
                kind="intent",
                repo_path="docs/sdlc/dark-mode/intent.md",
                commit_sha="abc1234",
            ))

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        asyncio.run(_seed(db_url))
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url,
                                     "CCSWITCH_DEV": "1",
                                     "CCSWITCH_TAILNET_ALLOWLIST": ""}):
            from server.sessions.orchestrator import SessionOrchestrator
            from server.tests.fakes.fake_factory import fake_sdk_factory
            from server.app import create_app
            from fastapi.testclient import TestClient

            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            app = create_app(orchestrator=orch)
            with TestClient(app, raise_server_exceptions=False) as c:
                r = c.get("/api/sdlc/initiatives", headers=IDENTITY_HEADERS)
                assert r.status_code == 200, r.text
                items = r.json()
                assert len(items) == 1
                artifacts = items[0]["artifacts"]
                assert len(artifacts) == 1
                assert artifacts[0]["kind"] == "intent"
                assert artifacts[0]["commitSha"] == "abc1234"
                assert artifacts[0]["repoPath"] == "docs/sdlc/dark-mode/intent.md"


def test_create_conflicting_slug_409():
    """Second POST with same slug → 409."""
    with _client() as c:
        r1 = c.post("/api/sdlc/initiatives",
                    json={"title": "Dark mode", "repo": "steiner385/ccswitch"},
                    headers=IDENTITY_HEADERS)
        assert r1.status_code == 201, r1.text

        r2 = c.post("/api/sdlc/initiatives",
                    json={"title": "Dark mode", "repo": "steiner385/ccswitch"},
                    headers=IDENTITY_HEADERS)
        assert r2.status_code == 409, r2.text


def test_get_detail_includes_chain_and_action():
    """Seed Initiative + intent artifact; GET detail → artifacts[0].kind=='intent',
    nextAction == {'type': 'await_approval', 'kind': 'intent'}."""
    from server.db import db_session
    from server.db.models import Initiative, InitiativeArtifact

    async def _seed(db_url: str):
        from server.db import init_db
        await init_db(db_url)
        async with db_session() as db:
            ini = Initiative(
                slug="dark-mode",
                repo="steiner385/ccswitch",
                title="Dark mode",
                stage="intake",
                status="proposed",
            )
            db.add(ini)
            await db.flush()
            db.add(InitiativeArtifact(
                initiative_id=ini.id,
                kind="intent",
                repo_path="docs/sdlc/dark-mode/intent.md",
            ))

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        asyncio.run(_seed(db_url))
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url,
                                     "CCSWITCH_DEV": "1",
                                     "CCSWITCH_TAILNET_ALLOWLIST": ""}):
            from server.sessions.orchestrator import SessionOrchestrator
            from server.tests.fakes.fake_factory import fake_sdk_factory
            from server.app import create_app
            from fastapi.testclient import TestClient

            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            app = create_app(orchestrator=orch)
            with TestClient(app, raise_server_exceptions=False) as c:
                r = c.get("/api/sdlc/initiatives/dark-mode", headers=IDENTITY_HEADERS)
                assert r.status_code == 200, r.text
                body = r.json()
                artifacts = body["artifacts"]
                assert len(artifacts) == 1
                assert artifacts[0]["kind"] == "intent"
                assert body["nextAction"] == {"type": "await_approval", "kind": "intent"}


def test_advance_moves_stage_and_activates():
    """POST advance: intake→design, status=='active'; advancing a done initiative → 409."""
    with _client() as c:
        c.post("/api/sdlc/initiatives",
               json={"title": "Dark mode", "repo": "steiner385/ccswitch"},
               headers=IDENTITY_HEADERS)

        r = c.post("/api/sdlc/initiatives/dark-mode/advance", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["stage"] == "design"
        assert body["status"] == "active"

        # Advance all the way to done
        for _ in range(10):  # enough to reach 'done'
            resp = c.post("/api/sdlc/initiatives/dark-mode/advance", headers=IDENTITY_HEADERS)
            if resp.json().get("stage") == "done":
                break

        # Advancing a done initiative → 409
        r2 = c.post("/api/sdlc/initiatives/dark-mode/advance", headers=IDENTITY_HEADERS)
        assert r2.status_code == 409, r2.text


def test_build_stage_surfaces_backlog_sessions():
    """Seed Initiative(stage='build', origin_ref='o/r#7') + a Session with
    origin='backlog-runner', origin_ref='o/r#7'; GET detail → buildSessions has that session id (#801)."""
    from server.db import db_session
    from server.db.models import Initiative, Session as DbSession

    async def _seed(db_url: str):
        from server.db import init_db
        await init_db(db_url)
        async with db_session() as db:
            ini = Initiative(
                slug="feature-x",
                repo="o/r",
                title="Feature X",
                stage="build",
                status="active",
                origin="backlog-runner",
                origin_ref="o/r#7",
            )
            db.add(ini)
            db.add(DbSession(
                id="sess-build-1",
                name="build-session",
                account_name="acct1",
                cwd="/tmp",
                origin="backlog-runner",
                origin_ref="o/r#7",
            ))

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        asyncio.run(_seed(db_url))
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url,
                                     "CCSWITCH_DEV": "1",
                                     "CCSWITCH_TAILNET_ALLOWLIST": ""}):
            from server.sessions.orchestrator import SessionOrchestrator
            from server.tests.fakes.fake_factory import fake_sdk_factory
            from server.app import create_app
            from fastapi.testclient import TestClient

            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            app = create_app(orchestrator=orch)
            with TestClient(app, raise_server_exceptions=False) as c:
                r = c.get("/api/sdlc/initiatives/feature-x", headers=IDENTITY_HEADERS)
                assert r.status_code == 200, r.text
                body = r.json()
                build_sessions = body["buildSessions"]
                ids = [s["id"] for s in build_sessions]
                assert "sess-build-1" in ids


def test_get_detail_includes_epic_progress():
    """Seed Initiative(epic_repo, epic_number) + an epic WorkItem with 3 children
    (1 closed, 1 'Blocked', 1 'Ready' via GhProjectItem.status); GET detail →
    epic.total==3, epic.done==1, epic.blocked==1, epic.needsHuman==0 (#991)."""
    from server.db import db_session
    from server.db.models import GhProjectItem, Initiative, WorkItem

    async def _seed(db_url: str):
        from server.db import init_db
        await init_db(db_url)
        async with db_session() as db:
            ini = Initiative(
                slug="the-epic",
                repo="o/r",
                title="The Epic",
                stage="build",
                status="active",
                origin="epic-adoption",
                origin_ref="o/r#997",
                epic_repo="o/r",
                epic_number=997,
            )
            db.add(ini)

            epic_wi = WorkItem(repo="o/r", number=997, kind="issue", state="open", is_epic=True)
            db.add(epic_wi)
            await db.flush()

            done_child = WorkItem(
                repo="o/r", number=1, kind="issue", title="Done child",
                state="closed", parent_work_item_id=epic_wi.id)
            blocked_child = WorkItem(
                repo="o/r", number=2, kind="issue", title="Blocked child",
                state="open", parent_work_item_id=epic_wi.id)
            ready_child = WorkItem(
                repo="o/r", number=3, kind="issue", title="Ready child",
                state="open", parent_work_item_id=epic_wi.id)
            db.add_all([done_child, blocked_child, ready_child])
            await db.flush()

            db.add(GhProjectItem(work_item_id=blocked_child.id, status="Blocked"))
            db.add(GhProjectItem(work_item_id=ready_child.id, status="Ready"))

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        asyncio.run(_seed(db_url))
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url,
                                     "CCSWITCH_DEV": "1",
                                     "CCSWITCH_TAILNET_ALLOWLIST": ""}):
            from server.sessions.orchestrator import SessionOrchestrator
            from server.tests.fakes.fake_factory import fake_sdk_factory
            from server.app import create_app
            from fastapi.testclient import TestClient

            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            app = create_app(orchestrator=orch)
            with TestClient(app, raise_server_exceptions=False) as c:
                r = c.get("/api/sdlc/initiatives/the-epic", headers=IDENTITY_HEADERS)
                assert r.status_code == 200, r.text
                body = r.json()
                assert body["epicRepo"] == "o/r"
                assert body["epicNumber"] == 997
                epic = body["epic"]
                assert epic["ref"] == "o/r#997"
                assert epic["url"] == "https://github.com/o/r/issues/997"
                assert epic["total"] == 3
                assert epic["done"] == 1
                assert epic["blocked"] == 1
                assert epic["needsHuman"] == 0
                child_refs = {c["ref"]: c for c in epic["children"]}
                assert child_refs["o/r#1"]["state"] == "closed"
                assert child_refs["o/r#2"]["status"] == "Blocked"
                assert child_refs["o/r#3"]["status"] == "Ready"

                # list endpoint carries the epic pointer too, but not the expanded epic dict
                listed = c.get("/api/sdlc/initiatives", headers=IDENTITY_HEADERS)
                assert listed.status_code == 200
                item = next(i for i in listed.json() if i["slug"] == "the-epic")
                assert item["epicRepo"] == "o/r"
                assert item["epicNumber"] == 997


def test_get_detail_no_epic_is_null():
    """Initiative with no epic_repo/epic_number → detail's epic field is null."""
    with _client() as c:
        c.post("/api/sdlc/initiatives",
               json={"title": "Dark mode", "repo": "steiner385/ccswitch"},
               headers=IDENTITY_HEADERS)
        r = c.get("/api/sdlc/initiatives/dark-mode", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["epicRepo"] is None
        assert body["epicNumber"] is None
        assert body["epic"] is None


def test_unauthenticated_401():
    """Request without identity headers → 401."""
    with _client() as c:
        assert c.get("/api/sdlc/initiatives").status_code == 401
        assert c.post("/api/sdlc/initiatives",
                      json={"title": "x", "repo": "r"}).status_code == 401
        assert c.get("/api/sdlc/initiatives/nope").status_code == 401
        assert c.post("/api/sdlc/initiatives/nope/advance").status_code == 401
        assert c.get("/api/sdlc/triage").status_code == 401


def test_triage_stub():
    """GET /api/sdlc/triage → {'items': []}."""
    with _client() as c:
        r = c.get("/api/sdlc/triage", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json() == {"items": []}
