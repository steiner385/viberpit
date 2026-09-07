"""GET /api/pm/map — the nested product → capability → initiative tree the
roadmap web renders from (spec 2026-09-07 §10). Mirrors test_pm_deps_api.py's
_client() fixture, but accepts a caller-prepared db_url so seeding can happen
BEFORE the client (and its own event loop) is opened — asyncio.run(_seed())
inside the open TestClient block races the app's own loop for the SQLAlchemy
engine's async Queue (#1386-shaped failure reproduced on this branch).
"""
from __future__ import annotations

import contextlib
import os
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}


@contextmanager
def _client(db_url: str | None = None):
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
    with contextlib.ExitStack() as stack:
        url = db_url
        if url is None:
            tmp = stack.enter_context(tempfile.TemporaryDirectory())
            url = f"sqlite+aiosqlite:///{tmp}/test.db"
        saved = {k: os.environ.pop(k, None) for k in _clear}
        try:
            with patch.dict(os.environ, {"CCSWITCH_DB_URL": url,
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


def test_map_returns_nested_capabilities_initiatives_and_tiers():
    import asyncio

    from server.db import db_session, init_db
    from server.db.models import (
        Feature, FeatureLink, Initiative, InitiativeFeature, Project, ProjectRepo,
        TriageProposal, WorkItem, WorkItemDep)

    async def _seed(db_url: str) -> None:
        await init_db(db_url)
        async with db_session() as db:
            infra = Project(slug="example-infra", name="Infra")
            prod = Project(slug="example-product", name="Example product")
            db.add_all([infra, prod])
            await db.flush()
            db.add(ProjectRepo(project_id=prod.id, repo="o/r", is_primary=True))
            mq = Feature(project_id=infra.id, slug="message-queue", title="MQ", state="live")
            sessions = Feature(project_id=prod.id, slug="sessions", title="Sessions", state="live")
            gone = Feature(project_id=prod.id, slug="gone", title="Gone", state="live", orphaned=True)
            db.add_all([mq, sessions, gone])
            await db.flush()
            dictation = Feature(project_id=prod.id, slug="dictation", title="Dictation",
                                state="delivering", parent_feature_id=sessions.id)
            db.add(dictation)
            await db.flush()
            db.add(FeatureLink(src_feature_id=dictation.id, dst_feature_id=mq.id, kind="powered_by"))
            # Two open issues under dictation: one blocked by an unmirrored issue, one plain;
            # one closed issue that must not count; one unclassified open issue (awaiting nothing).
            a = WorkItem(repo="o/r", number=1, kind="issue", state="open", title="A",
                         feature_id=dictation.id, triage_state="classified")
            b = WorkItem(repo="o/r", number=2, kind="issue", state="open", title="B",
                         feature_id=dictation.id, triage_state="classified")
            closed = WorkItem(repo="o/r", number=3, kind="issue", state="closed", title="C",
                              feature_id=dictation.id, triage_state="classified")
            pending = WorkItem(repo="o/r", number=4, kind="issue", state="open", title="D",
                               triage_state="proposed")
            rejected = WorkItem(repo="o/r", number=5, kind="issue", state="open", title="E",
                                triage_state="needs_human")
            db.add_all([a, b, closed, pending, rejected])
            await db.flush()
            db.add(WorkItemDep(src_work_item_id=a.id, src_ref="o/r#1", dst_ref="o/r#99",
                               source="native", active=True))
            db.add(TriageProposal(work_item_id=pending.id, status="pending"))
            ini = Initiative(slug="whisper", repo="o/r", title="Whisper", stage="build",
                             status="active", project_id=prod.id)
            db.add(ini)
            await db.flush()
            db.add(InitiativeFeature(initiative_id=ini.id, feature_id=dictation.id, is_primary=True))
            db.add(InitiativeFeature(initiative_id=ini.id, feature_id=sessions.id))
            await db.commit()

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        asyncio.run(_seed(db_url))

        with _client(db_url) as c:
            r = c.get("/api/pm/map", headers=IDENTITY_HEADERS)
            assert r.status_code == 200, r.text
            body = r.json()
            assert "generatedAt" in body
            products = {p["slug"]: p for p in body["products"]}
            prod = products["example-product"]
            roots = {cap["slug"]: cap for cap in prod["capabilities"]}
            assert set(roots) == {"sessions"}                      # orphaned `gone` excluded
            dictation = roots["sessions"]["children"][0]
            assert dictation["slug"] == "dictation"
            assert dictation["links"] == [{"kind": "powered_by", "projectSlug": "example-infra",
                                           "featureSlug": "message-queue"}]
            assert dictation["counts"]["openIssues"] == 2
            assert dictation["counts"]["blocked"] == 1
            assert dictation["initiatives"] == ["whisper"]
            assert dictation["tier"] == "blocked"                  # blocked issue outranks in-flight
            assert roots["sessions"]["initiatives"] == ["whisper"]
            assert prod["initiatives"][0]["primaryCapability"] == "dictation"
            assert prod["initiatives"][0]["capabilities"] == ["dictation", "sessions"] or \
                   prod["initiatives"][0]["capabilities"] == ["sessions", "dictation"]
            assert prod["counts"]["awaitingTriage"] == 1
            assert prod["counts"]["needsHome"] == 1
            assert prod["counts"]["capabilities"] == 2
            assert prod["tier"] == "needs-you"
            assert products["example-infra"]["capabilities"][0]["slug"] == "message-queue"


def test_map_is_empty_without_products():
    with _client() as c:
        r = c.get("/api/pm/map", headers=IDENTITY_HEADERS)
        assert r.status_code == 200
        assert r.json()["products"] == []
