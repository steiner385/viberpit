"""`GET /api/pm/map` carries `parentInitiative`/`hasChildren` on initiative
nodes so the web can nest a program over its phases (roadmap 5S plan)."""
from __future__ import annotations

import asyncio
import os
import tempfile
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
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url, "CCSWITCH_DEV": "1",
                                     "CCSWITCH_TAILNET_ALLOWLIST": ""}):
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
