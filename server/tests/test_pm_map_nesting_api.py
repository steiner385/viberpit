"""`GET /api/pm/map` carries `parentInitiative`/`hasChildren` on initiative
nodes so the web can nest a program over its phases (roadmap 5S plan)."""
from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch

IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}


def test_map_initiative_nodes_carry_their_parent():
    from server.db import db_session, init_db
    from server.db.models import Feature, Initiative, InitiativeFeature, Project, ProjectRepo

    async def _seed(db_url):
        await init_db(db_url)
        async with db_session() as db:
            p = Project(slug="kindash", name="KinDash", onboarding_state="live")
            db.add(p)
            await db.flush()
            db.add(ProjectRepo(project_id=p.id, repo="o/r", is_primary=True))
            f = Feature(project_id=p.id, slug="accessibility", title="Accessibility", state="live")
            db.add(f)
            await db.flush()
            # A stale root (proposed, long untouched) while `accessibility` gains a
            # child below — so the tree is "in use" and stale-leaf is the loose one.
            db.add(Feature(project_id=p.id, slug="stale-leaf", title="Stale", state="proposed",
                           updated_at=datetime(2026, 7, 1, tzinfo=timezone.utc)))
            db.add(Feature(project_id=p.id, slug="child", parent_feature_id=f.id,
                           title="Child", state="live"))
            await db.flush()
            prog = Initiative(slug="audit", title="UI audit", stage="design", status="active", project_id=p.id)
            db.add(prog)
            await db.flush()
            ph = Initiative(slug="audit-01", title="01", stage="plan", status="active",
                            project_id=p.id, parent_initiative_id=prog.id)
            db.add(ph)
            await db.flush()
            db.add_all([InitiativeFeature(initiative_id=prog.id, feature_id=f.id, is_primary=True),
                        InitiativeFeature(initiative_id=ph.id, feature_id=f.id, is_primary=True)])

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/t.db"
        asyncio.run(_seed(db_url))
        with patch.dict(os.environ, {"VIBERPIT_DB_URL": db_url, "VIBERPIT_DEV": "1",
                                     "VIBERPIT_TAILNET_ALLOWLIST": ""}):
            from fastapi.testclient import TestClient
            from server.app import create_app
            from server.sessions.orchestrator import SessionOrchestrator
            from server.tests.fakes.fake_factory import fake_sdk_factory
            app = create_app(orchestrator=SessionOrchestrator(sdk_factory=fake_sdk_factory()))
            with TestClient(app, raise_server_exceptions=False) as c:
                prod = c.get("/api/pm/map", headers=IDENTITY_HEADERS).json()["products"][0]
                nodes = {i["slug"]: i for i in prod["initiatives"]}
                assert nodes["audit"]["hasChildren"] is True and nodes["audit"]["parentInitiative"] is None
                assert nodes["audit-01"]["parentInitiative"] == "audit"
                assert nodes["audit"]["stage"] == "plan"
                assert prod["capabilities"][0]["counts"]["inFlight"] == 1   # the phase only

                health = c.get("/api/pm/projects/kindash/health", headers=IDENTITY_HEADERS).json()
                assert health["unlinkedInitiatives"] == []   # a program is judged by its phases, the phase is linked
                assert health["staleInbox"] == 0
                # accessibility now has a child, so the tree is in use and stale-leaf
                # (a root with none) is the loose one — read back as a naive UTC
                # timestamp from SQLite, which is the path under test.
                assert health["unparentedCapabilities"] == ["stale-leaf"]
                assert health["staleProposed"] == ["stale-leaf"]
                assert health["unindexedDocs"] == []


def test_project_health_surfaces_governance_status():
    """`GET /api/pm/projects/{slug}/health` passes `Project.constitution_status`
    through as the `governance` field (issue #2165)."""
    from server.db import db_session, init_db
    from server.db.models import Project

    async def _seed(db_url):
        await init_db(db_url)
        async with db_session() as db:
            db.add(Project(slug="kindash", name="KinDash", onboarding_state="live",
                           constitution_status="missing"))

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/t.db"
        asyncio.run(_seed(db_url))
        with patch.dict(os.environ, {"VIBERPIT_DB_URL": db_url, "VIBERPIT_DEV": "1",
                                     "VIBERPIT_TAILNET_ALLOWLIST": ""}):
            from fastapi.testclient import TestClient
            from server.app import create_app
            from server.sessions.orchestrator import SessionOrchestrator
            from server.tests.fakes.fake_factory import fake_sdk_factory
            app = create_app(orchestrator=SessionOrchestrator(sdk_factory=fake_sdk_factory()))
            with TestClient(app, raise_server_exceptions=False) as c:
                health = c.get("/api/pm/projects/kindash/health", headers=IDENTITY_HEADERS).json()
                assert health["governance"] == "missing"


def test_health_endpoint_includes_unlinked_personas():
    """A capability's `persona_slugs` (#2164 `personas:` frontmatter) naming a
    slug with no matching live `Persona` row surfaces as `unlinkedPersonas`."""
    from server.db import db_session, init_db
    from server.db.models import Feature, Project

    async def _seed(db_url):
        await init_db(db_url)
        async with db_session() as db:
            p = Project(slug="kindash", name="KinDash", onboarding_state="live")
            db.add(p)
            await db.flush()
            db.add(Feature(project_id=p.id, slug="chores", title="Chores", state="live",
                           persona_slugs=["ghost"]))

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/t.db"
        asyncio.run(_seed(db_url))
        with patch.dict(os.environ, {"VIBERPIT_DB_URL": db_url, "VIBERPIT_DEV": "1",
                                     "VIBERPIT_TAILNET_ALLOWLIST": ""}):
            from fastapi.testclient import TestClient
            from server.app import create_app
            from server.sessions.orchestrator import SessionOrchestrator
            from server.tests.fakes.fake_factory import fake_sdk_factory
            app = create_app(orchestrator=SessionOrchestrator(sdk_factory=fake_sdk_factory()))
            with TestClient(app, raise_server_exceptions=False) as c:
                health = c.get("/api/pm/projects/kindash/health", headers=IDENTITY_HEADERS).json()
                assert health["unlinkedPersonas"] == [{"capability": "chores", "persona": "ghost"}]


def test_personas_endpoint_lists_indexed_personas():
    """`GET /api/pm/projects/{slug}/personas` lists non-orphaned `Persona`
    rows for a product (#2164)."""
    from server.db import db_session, init_db
    from server.db.models import Persona, Project

    async def _seed(db_url):
        await init_db(db_url)
        async with db_session() as db:
            p = Project(slug="kindash", name="KinDash", onboarding_state="live")
            db.add(p)
            await db.flush()
            db.add(Persona(project_id=p.id, slug="teen", title="Teen", role="teen"))

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/t.db"
        asyncio.run(_seed(db_url))
        with patch.dict(os.environ, {"VIBERPIT_DB_URL": db_url, "VIBERPIT_DEV": "1",
                                     "VIBERPIT_TAILNET_ALLOWLIST": ""}):
            from fastapi.testclient import TestClient
            from server.app import create_app
            from server.sessions.orchestrator import SessionOrchestrator
            from server.tests.fakes.fake_factory import fake_sdk_factory
            app = create_app(orchestrator=SessionOrchestrator(sdk_factory=fake_sdk_factory()))
            with TestClient(app, raise_server_exceptions=False) as c:
                resp = c.get("/api/pm/projects/kindash/personas", headers=IDENTITY_HEADERS)
                assert resp.json() == [{"slug": "teen", "title": "Teen", "role": "teen",
                                        "ageBand": None, "tier": None, "summary": None}]


def test_leaves_of_a_program_lists_the_phases_leaf_issue_not_the_sub_epic():
    """`/api/pm/leaves?initiative=<program>` must match `epic.total` (the leaf
    roll-up): the direct sub-issues of a program's own epic are its phases'
    sub-EPICS, not leaves, so it lists the direct sub-issues of every leaf
    phase's epic instead."""
    from server.db import db_session, init_db
    from server.db.models import Initiative, Project, ProjectRepo, WorkItem

    async def _seed(db_url):
        await init_db(db_url)
        async with db_session() as db:
            p = Project(slug="kindash", name="KinDash", onboarding_state="live")
            db.add(p)
            await db.flush()
            db.add(ProjectRepo(project_id=p.id, repo="o/r", is_primary=True))
            program = Initiative(slug="audit", title="UI audit", stage="design", status="active",
                                 project_id=p.id, epic_repo="o/r", epic_number=100)
            db.add(program)
            await db.flush()
            phase = Initiative(slug="audit-01", title="01", stage="plan", status="active",
                               project_id=p.id, parent_initiative_id=program.id,
                               epic_repo="o/r", epic_number=101)
            db.add(phase)
            await db.flush()
            prog_wi = WorkItem(repo="o/r", number=100, kind="issue", state="open", is_epic=True)
            db.add(prog_wi)
            await db.flush()
            phase_epic = WorkItem(repo="o/r", number=101, kind="issue", state="open",
                                  is_epic=True, parent_work_item_id=prog_wi.id)
            db.add(phase_epic)
            await db.flush()
            db.add(WorkItem(repo="o/r", number=102, kind="issue", state="open",
                            parent_work_item_id=phase_epic.id))

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/t.db"
        asyncio.run(_seed(db_url))
        with patch.dict(os.environ, {"VIBERPIT_DB_URL": db_url, "VIBERPIT_DEV": "1",
                                     "VIBERPIT_TAILNET_ALLOWLIST": ""}):
            from fastapi.testclient import TestClient
            from server.app import create_app
            from server.sessions.orchestrator import SessionOrchestrator
            from server.tests.fakes.fake_factory import fake_sdk_factory
            app = create_app(orchestrator=SessionOrchestrator(sdk_factory=fake_sdk_factory()))
            with TestClient(app, raise_server_exceptions=False) as c:
                body = c.get("/api/pm/leaves", params={"initiative": "audit"},
                             headers=IDENTITY_HEADERS).json()
                assert [item["number"] for item in body["items"]] == [102]
