"""/api/sdlc router + build handoff correlation (#801, #802)."""
from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}


@contextmanager
def _client(db_url: str | None = None):
    """Same idiom as ``test_pm_map_api.py::_client``: accept a caller-prepared
    ``db_url`` so seeding can happen BEFORE the client (and its own event loop)
    is opened, or build its own temp dir when none is given."""
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    with contextlib.ExitStack() as stack:
        url = db_url
        if url is None:
            tmp = stack.enter_context(tempfile.TemporaryDirectory())
            url = f"sqlite+aiosqlite:///{tmp}/test.db"
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": url,
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

                # The list endpoint serves the API's non-web readers (the AI
                # assistant's `server/assistant/tools.py`, and anything scripting
                # /api/sdlc/initiatives) — it must carry the *expanded* epic dict
                # too, computed via one grouped query, not just the epicRepo/
                # epicNumber pointer (#991 review round 1, CRITICAL 1). It omits
                # `children` though (#991 final review, R8/Finding 2): no list
                # reader uses the per-child array. The web roadmap reads neither
                # — progress comes from /api/pm/map, and the initiative page
                # fetches the per-slug detail route — so the list's epic dict
                # equals the detail one minus that one key.
                listed = c.get("/api/sdlc/initiatives", headers=IDENTITY_HEADERS)
                assert listed.status_code == 200
                item = next(i for i in listed.json() if i["slug"] == "the-epic")
                assert item["epicRepo"] == "o/r"
                assert item["epicNumber"] == 997
                assert "children" not in item["epic"]
                assert item["epic"] == {k: v for k, v in epic.items() if k != "children"}


def test_list_epic_progress_dedupes_child_mirrored_onto_two_boards():
    """A child WorkItem with GhProjectItem rows on two different boards (nothing
    in the schema constrains a work item to one project — GhProjectItem's only
    unique constraint is (gh_project_id, gh_item_node_id)) must still count
    once, not twice: total stays 3, and the most-recently-updated status wins
    (#991 review round 1, IMPORTANT 3). Asserted through the LIST endpoint,
    which omits `children` (#991 final review, Finding 2) — the aggregate
    counts (total/blocked/done) are what dedup affects and what the board
    reads, so they're checked directly rather than via the per-child array."""
    from datetime import datetime, timedelta, timezone

    from server.db import db_session
    from server.db.models import GhProjectItem, Initiative, WorkItem

    async def _seed(db_url: str):
        from server.db import init_db
        await init_db(db_url)
        async with db_session() as db:
            ini = Initiative(
                slug="the-epic", repo="o/r", title="The Epic", stage="build", status="active",
                origin="epic-adoption", origin_ref="o/r#997", epic_repo="o/r", epic_number=997,
            )
            db.add(ini)
            epic_wi = WorkItem(repo="o/r", number=997, kind="issue", state="open", is_epic=True)
            db.add(epic_wi)
            await db.flush()

            done_child = WorkItem(repo="o/r", number=1, kind="issue", state="closed",
                                  parent_work_item_id=epic_wi.id)
            dup_child = WorkItem(repo="o/r", number=2, kind="issue", state="open",
                                 parent_work_item_id=epic_wi.id)
            ready_child = WorkItem(repo="o/r", number=3, kind="issue", state="open",
                                   parent_work_item_id=epic_wi.id)
            db.add_all([done_child, dup_child, ready_child])
            await db.flush()

            now = datetime.now(timezone.utc)
            # dup_child mirrored onto two boards with two different statuses;
            # the later-updated row ("Blocked") must be the one that wins.
            db.add(GhProjectItem(work_item_id=dup_child.id, gh_project_id="board-a",
                                 status="Ready", updated_at=now - timedelta(minutes=5)))
            db.add(GhProjectItem(work_item_id=dup_child.id, gh_project_id="board-b",
                                 status="Blocked", updated_at=now))

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
                epic = next(i for i in r.json() if i["slug"] == "the-epic")["epic"]
                assert epic["total"] == 3, "duplicate board row must not inflate total"
                assert epic["blocked"] == 1, "later-updated status ('Blocked') must win"
                assert epic["done"] == 1
                assert "children" not in epic


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


def test_detail_next_action_materialize_depends_on_the_epic_pointer():
    """A merged plan with no epic → nextAction 'materialize' (#992); the same
    initiative WITH an epic pointer keeps the pre-existing await_approval. The
    detail endpoint builds its own InitiativeView, so it has to derive `epic`
    from the row or it would report materialize forever."""
    from server.db import db_session
    from server.db.models import Initiative, InitiativeArtifact

    async def _seed(db_url: str):
        from server.db import init_db
        await init_db(db_url)
        async with db_session() as db:
            for slug, epic_repo, epic_number in (("no-epic", None, None),
                                                 ("has-epic", "o/r", 42)):
                ini = Initiative(slug=slug, repo="o/r", title=slug, stage="plan",
                                 status="active", epic_repo=epic_repo,
                                 epic_number=epic_number)
                db.add(ini)
                await db.flush()
                db.add(InitiativeArtifact(
                    initiative_id=ini.id, kind="plan",
                    repo_path=f"docs/sdlc/{slug}/plan.md", commit_sha="deadbeef",
                    pr_number=7))

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
                r = c.get("/api/sdlc/initiatives/no-epic", headers=IDENTITY_HEADERS)
                assert r.status_code == 200, r.text
                assert r.json()["nextAction"] == {"type": "materialize", "kind": "plan"}

                r2 = c.get("/api/sdlc/initiatives/has-epic", headers=IDENTITY_HEADERS)
                assert r2.status_code == 200, r2.text
                body = r2.json()
                assert body["nextAction"] == {"type": "await_approval", "kind": "plan"}
                # The wire `epic` key stays the progress object, never the
                # engine view's internal "o/r#42" ref.
                assert body["epic"] is None or isinstance(body["epic"], dict)


def test_unauthenticated_401():
    """Request without identity headers → 401."""
    with _client() as c:
        assert c.get("/api/sdlc/initiatives").status_code == 401
        assert c.post("/api/sdlc/initiatives",
                      json={"title": "x", "repo": "r"}).status_code == 401
        assert c.get("/api/sdlc/initiatives/nope").status_code == 401
        assert c.post("/api/sdlc/initiatives/nope/advance").status_code == 401
        assert c.get("/api/sdlc/triage").status_code == 401
        assert c.post("/api/sdlc/triage/x/action",
                      json={"action": "dismiss", "reason": "r"}).status_code == 401


def test_triage_queue_is_empty_before_any_band_breaches():
    """The queue is real (#804) but starts empty — the control-band monitor is
    what fills it. Behaviour with rows lives in test_sdlc_triage_api.py."""
    with _client() as c:
        r = c.get("/api/sdlc/triage", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json() == {"items": []}


def test_create_with_capabilities_links_and_derives_product():
    """Seed the product + capabilities into the SAME db_url the TestClient will use,
    in one asyncio.run() BEFORE the client (and its own event loop) is opened —
    mirrors test_list_includes_artifact_chain above. Seeding via a separate
    asyncio.run() *inside* the `with _client():` block would run against a
    different db_url and race the app's own loop over the aiosqlite pool."""
    from server.db import db_session
    from server.db.models import Feature, Project

    async def _seed(db_url: str) -> tuple[str, str, str]:
        from server.db import init_db
        await init_db(db_url)
        async with db_session() as db:
            p = Project(slug="example-product", name="Example product")
            db.add(p)
            await db.flush()
            root = Feature(project_id=p.id, slug="sessions", title="Sessions", state="live")
            db.add(root)
            await db.flush()
            child = Feature(project_id=p.id, slug="dictation", title="Dictation",
                            state="delivering", parent_feature_id=root.id)
            db.add(child)
            await db.commit()
            return p.id, root.id, child.id

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        _, root_id, child_id = asyncio.run(_seed(db_url))
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
                r = c.post("/api/sdlc/initiatives", json={
                    "title": "Whisper on the edge", "repo": "o/r",
                    "primaryCapabilityId": child_id, "capabilityIds": [root_id]},
                    headers=IDENTITY_HEADERS)
                assert r.status_code == 201, r.text
                body = r.json()
                assert body["projectSlug"] == "example-product"
                assert body["primaryCapability"] == {"id": child_id, "slug": "dictation", "title": "Dictation"}
                assert sorted((x["slug"], x["isPrimary"]) for x in body["capabilities"]) == [
                    ("dictation", True), ("sessions", False)]

                listed = c.get("/api/sdlc/initiatives", headers=IDENTITY_HEADERS).json()
                assert listed[0]["primaryCapability"]["slug"] == "dictation"
                detail = c.get("/api/sdlc/initiatives/whisper-on-the-edge", headers=IDENTITY_HEADERS).json()
                assert detail["projectSlug"] == "example-product"
                assert len(detail["capabilities"]) == 2


def test_create_with_unknown_capability_is_422():
    with _client() as c:
        r = c.post("/api/sdlc/initiatives", json={
            "title": "X", "repo": "o/r", "capabilityIds": ["nope"]}, headers=IDENTITY_HEADERS)
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "capability_not_found"


def test_create_with_capabilities_spanning_products_is_422():
    """A capability set touching two products is rejected: an initiative has one
    projectSlug (derived from its capabilities' project), so a cross-product set
    would leave that derivation ambiguous."""
    from server.db import db_session
    from server.db.models import Feature, Project

    async def _seed(db_url: str) -> tuple[str, str]:
        from server.db import init_db
        await init_db(db_url)
        async with db_session() as db:
            p1 = Project(slug="product-one", name="Product one")
            p2 = Project(slug="product-two", name="Product two")
            db.add_all([p1, p2])
            await db.flush()
            f1 = Feature(project_id=p1.id, slug="alpha", title="Alpha", state="live")
            f2 = Feature(project_id=p2.id, slug="beta", title="Beta", state="live")
            db.add_all([f1, f2])
            await db.commit()
            return f1.id, f2.id

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        f1_id, f2_id = asyncio.run(_seed(db_url))
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
                r = c.post("/api/sdlc/initiatives", json={
                    "title": "Cross product", "repo": "o/r",
                    "capabilityIds": [f1_id, f2_id]}, headers=IDENTITY_HEADERS)
                assert r.status_code == 422, r.text
                assert r.json()["detail"]["error"] == "capabilities_span_products"


def test_initiative_without_capabilities_has_null_fields():
    with _client() as c:
        r = c.post("/api/sdlc/initiatives", json={"title": "Plain", "repo": "o/r"},
                   headers=IDENTITY_HEADERS)
        body = r.json()
        assert body["projectSlug"] is None
        assert body["primaryCapability"] is None
        assert body["capabilities"] == []


def _seed_product_with_two_capabilities(db_url: str) -> tuple[str, str, str]:
    """A product with a root capability and one child under it; returns
    (project_id, root_id, child_id). Seeded BEFORE the client opens, like
    ``test_create_with_capabilities_links_and_derives_product``."""
    from server.db import db_session, init_db
    from server.db.models import Feature, Project

    async def _go():
        await init_db(db_url)
        async with db_session() as db:
            p = Project(slug="example-product", name="Example product")
            db.add(p)
            await db.flush()
            root = Feature(project_id=p.id, slug="sessions", title="Sessions", state="live")
            db.add(root)
            await db.flush()
            child = Feature(project_id=p.id, slug="dictation", title="Dictation",
                            state="delivering", parent_feature_id=root.id)
            db.add(child)
            await db.commit()
            return p.id, root.id, child.id
    return asyncio.run(_go())


def test_patch_title_trims_and_audits():
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        _seed_initiative(db_url)
        with _client(db_url) as c:
            r = c.patch("/api/sdlc/initiatives/dark-mode", json={"title": "  Dark mode v2  "},
                        headers=IDENTITY_HEADERS)
            assert r.status_code == 200, r.text
            assert r.json()["title"] == "Dark mode v2"
            assert r.json()["stage"] == "plan"            # untouched
            assert _audits(c, "sdlc.initiative.update") == [{"slug": "dark-mode", "fields": ["title"]}]
            assert c.get("/api/sdlc/initiatives/dark-mode", headers=IDENTITY_HEADERS).json()["title"] == "Dark mode v2"


def test_patch_empty_body_is_a_read_through_and_blank_title_is_422():
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        _seed_initiative(db_url)
        with _client(db_url) as c:
            r = c.patch("/api/sdlc/initiatives/dark-mode", json={}, headers=IDENTITY_HEADERS)
            assert r.status_code == 200 and r.json()["title"] == "Dark mode"
            assert _audits(c, "sdlc.initiative.update") == []
            r = c.patch("/api/sdlc/initiatives/dark-mode", json={"title": "   "}, headers=IDENTITY_HEADERS)
            assert r.status_code == 422 and r.json()["detail"]["error"] == "bad_title"
            assert c.patch("/api/sdlc/initiatives/nope", json={"title": "x"},
                           headers=IDENTITY_HEADERS).status_code == 404


def test_patch_capabilities_replaces_the_set_and_rederives_the_product():
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        _, root_id, child_id = _seed_product_with_two_capabilities(db_url)
        with _client(db_url) as c:
            created = c.post("/api/sdlc/initiatives", json={
                "title": "Whisper", "repo": "o/r", "capabilityIds": [root_id]},
                headers=IDENTITY_HEADERS)
            assert created.status_code == 201, created.text
            assert created.json()["primaryCapability"] is None
            r = c.patch("/api/sdlc/initiatives/whisper", json={
                "primaryCapabilityId": child_id, "capabilityIds": [child_id]},
                headers=IDENTITY_HEADERS)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["projectSlug"] == "example-product"
            assert body["primaryCapability"]["slug"] == "dictation"
            assert [(x["slug"], x["isPrimary"]) for x in body["capabilities"]] == [("dictation", True)]
            assert _audits(c, "sdlc.initiative.update") == [
                {"slug": "whisper", "fields": ["capabilityIds", "primaryCapabilityId"]}]


def test_patch_primary_alone_joins_the_current_set_and_dropping_it_clears_it():
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        _, root_id, child_id = _seed_product_with_two_capabilities(db_url)
        with _client(db_url) as c:
            c.post("/api/sdlc/initiatives", json={
                "title": "Whisper", "repo": "o/r", "capabilityIds": [root_id]}, headers=IDENTITY_HEADERS)
            # primary alone: joins the current {root} set
            r = c.patch("/api/sdlc/initiatives/whisper", json={"primaryCapabilityId": child_id},
                        headers=IDENTITY_HEADERS)
            assert r.status_code == 200, r.text
            assert sorted((x["slug"], x["isPrimary"]) for x in r.json()["capabilities"]) == [
                ("dictation", True), ("sessions", False)]
            # a new set that omits the primary: the primary is gone, the product follows the first
            r = c.patch("/api/sdlc/initiatives/whisper", json={"capabilityIds": [root_id]},
                        headers=IDENTITY_HEADERS)
            assert r.json()["primaryCapability"] is None
            assert [x["slug"] for x in r.json()["capabilities"]] == ["sessions"]
            assert r.json()["projectSlug"] == "example-product"
            # an empty set: no capabilities, no product
            r = c.patch("/api/sdlc/initiatives/whisper", json={"capabilityIds": []}, headers=IDENTITY_HEADERS)
            assert r.json()["capabilities"] == [] and r.json()["projectSlug"] is None


def test_patch_rejects_unknown_and_cross_product_capabilities():
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        _, root_id, _ = _seed_product_with_two_capabilities(db_url)
        with _client(db_url) as c:
            c.post("/api/sdlc/initiatives", json={"title": "Whisper", "repo": "o/r"}, headers=IDENTITY_HEADERS)
            r = c.patch("/api/sdlc/initiatives/whisper", json={"capabilityIds": ["nope"]}, headers=IDENTITY_HEADERS)
            assert r.status_code == 422 and r.json()["detail"]["error"] == "capability_not_found"
            # a second product's capability, seeded through the client's own loop
            from server.db import db_session
            from server.db.models import Feature, Project

            async def _other():
                async with db_session() as db:
                    p = Project(slug="other", name="Other")
                    db.add(p)
                    await db.flush()
                    f = Feature(project_id=p.id, slug="billing", title="Billing", state="live")
                    db.add(f)
                    await db.commit()
                    return f.id
            other_id = c.portal.call(_other)
            r = c.patch("/api/sdlc/initiatives/whisper", json={"capabilityIds": [root_id, other_id]},
                        headers=IDENTITY_HEADERS)
            assert r.status_code == 422 and r.json()["detail"]["error"] == "capabilities_span_products"


# --------------------------------------------------------------------------- #
# Plan materialization — the human gate (spec 003 §7b, #992)                   #
# --------------------------------------------------------------------------- #

_MAT_PAYLOAD = {
    "epic_title": "Dark mode",
    "epic_body": "Ship dark mode.",
    "tasks": [{"title": "Tokens", "body": "b0", "depends_on": []},
              {"title": "Toggle", "body": "b1", "depends_on": [0]}],
    "source_session": "sess-9",
}


@contextmanager
def _materialization_client(*, proposals=(("pending", None),),
                            status: str = "awaiting_human",
                            applied: dict | None = None):
    """Client + orchestrator for an initiative carrying *proposals*.

    Each entry is ``(status, created_at)``; the orchestrator is yielded so a test
    can stub the apply seam — the router must never build a GitHub call itself.
    ``applied`` is the ``created`` map a partial apply would have left on the
    NEWEST proposal, and *status* the status that apply would have left behind.
    """
    from datetime import datetime, timedelta, timezone
    from server.db import db_session, init_db
    from server.db.models import Initiative, MaterializationProposal

    async def _seed(db_url: str):
        await init_db(db_url)
        async with db_session() as db:
            ini = Initiative(slug="dark-mode", repo="o/r", title="Dark mode",
                             stage="plan", status=status)
            db.add(ini)
            await db.flush()
            base = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
            for n, (st, created) in enumerate(proposals):
                payload: dict = dict(_MAT_PAYLOAD)
                if applied is not None and n == len(proposals) - 1:
                    payload["created"] = applied
                db.add(MaterializationProposal(
                    initiative_id=ini.id, status=st, session_id=f"sess-{n}",
                    rationale=f"because {n}", payload=payload,
                    created_at=created or (base + timedelta(minutes=n))))
            await db.commit()

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        asyncio.run(_seed(db_url))
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url,
                                     "CCSWITCH_DEV": "1",
                                     "CCSWITCH_TAILNET_ALLOWLIST": ""}):
            from fastapi.testclient import TestClient
            from server.app import create_app
            from server.sessions.orchestrator import SessionOrchestrator
            from server.tests.fakes.fake_factory import fake_sdk_factory

            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            app = create_app(orchestrator=orch)
            with TestClient(app, raise_server_exceptions=False) as c:
                yield c, orch


def _accept_seam(orch, *, ok: bool = True):
    """Stand-in for ``_sdlc_materialize_apply`` that records its calls and moves
    the proposal the way the real seam's apply does."""
    from server.db import db_session
    from server.db.models import Initiative, MaterializationProposal
    from sqlalchemy import select

    calls: list[str] = []

    async def fake(proposal_id: str) -> dict:
        calls.append(proposal_id)
        if not ok:
            return {"ok": False, "step": "task:1", "error": "cannot create Toggle"}
        async with db_session() as db:
            prop = (await db.execute(select(MaterializationProposal).where(
                MaterializationProposal.id == proposal_id))).scalar_one()
            prop.status = "accepted"
            # The real apply advances the stage in the same overlay (spec §7b) and
            # reports it back for the route's audit row.
            ini = (await db.execute(select(Initiative).where(
                Initiative.id == prop.initiative_id))).scalar_one()
            ini.stage, ini.status = "build", "active"
            await db.commit()
        return {"ok": True, "epic": {"number": 101}, "tasks": 2, "edges": 1,
                "stage": "build"}

    orch._sdlc_materialize_apply = fake
    return calls


def test_materialization_get_404_without_a_proposal():
    with _materialization_client(proposals=()) as (c, _orch):
        r = c.get("/api/sdlc/initiatives/dark-mode/materialization",
                  headers=IDENTITY_HEADERS)
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["error"] == "no_proposal"


def test_materialization_get_returns_the_latest_proposal_of_any_status():
    """A rejected proposal still has to be readable — the human needs to see what
    they turned down next to whatever replaced it."""
    with _materialization_client(proposals=(("rejected", None), ("pending", None))) \
            as (c, _orch):
        r = c.get("/api/sdlc/initiatives/dark-mode/materialization",
                  headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "pending"
        assert body["epicTitle"] == "Dark mode"
        assert [t["title"] for t in body["tasks"]] == ["Tokens", "Toggle"]
        assert body["tasks"][1]["dependsOn"] == [0]
        assert body["sessionId"] == "sess-1"
        assert body["rationale"] == "because 1"


def test_materialization_accept_runs_the_seam_and_409s_on_a_second_tap():
    with _materialization_client() as (c, orch):
        calls = _accept_seam(orch)
        r = c.post("/api/sdlc/initiatives/dark-mode/materialization/accept",
                   headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["tasks"] == 2
        assert len(calls) == 1

        again = c.post("/api/sdlc/initiatives/dark-mode/materialization/accept",
                       headers=IDENTITY_HEADERS)
        assert again.status_code == 409, again.text
        assert again.json()["detail"]["error"] == "not_pending"
        assert len(calls) == 1  # the second tap never reached GitHub


def test_materialization_accept_502_leaves_the_proposal_pending():
    """A partial apply is resumable: the proposal must stay pending so the same
    accept can be tapped again once the cause is fixed."""
    with _materialization_client() as (c, orch):
        _accept_seam(orch, ok=False)
        r = c.post("/api/sdlc/initiatives/dark-mode/materialization/accept",
                   headers=IDENTITY_HEADERS)
        assert r.status_code == 502, r.text
        assert r.json()["detail"]["error"] == "materialize_failed"
        assert r.json()["detail"]["step"] == "task:1"

        after = c.get("/api/sdlc/initiatives/dark-mode/materialization",
                      headers=IDENTITY_HEADERS)
        assert after.json()["status"] == "pending"


def test_materialization_reject_resolves_it_and_leaves_the_initiative_parked():
    with _materialization_client() as (c, orch):
        calls = _accept_seam(orch)
        r = c.post("/api/sdlc/initiatives/dark-mode/materialization/reject",
                   headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "rejected"
        assert r.json()["resolvedAt"]
        assert calls == []

        detail = c.get("/api/sdlc/initiatives/dark-mode", headers=IDENTITY_HEADERS)
        # NOT unparked: a rejection only frees the next tick to decompose again.
        assert detail.json()["status"] == "awaiting_human"

        again = c.post("/api/sdlc/initiatives/dark-mode/materialization/reject",
                       headers=IDENTITY_HEADERS)
        assert again.status_code == 409, again.text


def _audits(c, type_: str) -> list[dict]:
    """Every audit row of *type_*, oldest first. No GET /api/audit route exists, so
    this reads the DB through the client's own loop — the idiom in
    ``test_pm_api.test_triage_undo_writes_audit_row``."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import AuditEvent

    async def _read():
        async with db_session() as db:
            return [dict(r.detail or {}) for r in (await db.execute(
                select(AuditEvent).where(AuditEvent.type == type_)
                .order_by(AuditEvent.at))).scalars().all()]

    return c.portal.call(_read)


def test_materialization_accept_advances_the_initiative_to_build():
    """Materialize is the plan stage's TERMINAL action (spec §7b): the confirmed
    accept IS that gate, so the stage moves and the trail carries the SAME
    ``sdlc.initiative.advance`` row the Advance route writes."""
    with _materialization_client() as (c, orch):
        _accept_seam(orch)
        r = c.post("/api/sdlc/initiatives/dark-mode/materialization/accept",
                   headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["stage"] == "build"

        detail = c.get("/api/sdlc/initiatives/dark-mode", headers=IDENTITY_HEADERS)
        assert detail.json()["stage"] == "build"
        assert detail.json()["status"] == "active"

        assert _audits(c, "sdlc.initiative.advance") == [
            {"slug": "dark-mode", "stage": "build", "reason": "materialize"}]


def test_a_failed_accept_writes_no_advance():
    with _materialization_client() as (c, orch):
        _accept_seam(orch, ok=False)
        assert c.post("/api/sdlc/initiatives/dark-mode/materialization/accept",
                      headers=IDENTITY_HEADERS).status_code == 502
        assert _audits(c, "sdlc.initiative.advance") == []
        # …but the attempt itself is still on the trail.
        assert [a["ok"] for a in _audits(c, "sdlc.materialization.accept")] == [False]


def test_materialization_accept_409s_while_an_apply_is_in_flight():
    """C4-R17: the seam serialises per proposal, and an apply already running is a
    CONFLICT — the same write is in flight, so the answer is 'wait', not 'retry'."""
    with _materialization_client() as (c, orch):
        async def busy(proposal_id: str) -> dict:
            return {"ok": False, "step": "in_flight",
                    "error": "an apply for this proposal is already running"}

        orch._sdlc_materialize_apply = busy
        r = c.post("/api/sdlc/initiatives/dark-mode/materialization/accept",
                   headers=IDENTITY_HEADERS)
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "apply_in_flight"
        assert "already running" in r.json()["detail"]["message"]

        # Still pending and still resumable — nothing about the proposal changed.
        after = c.get("/api/sdlc/initiatives/dark-mode/materialization",
                      headers=IDENTITY_HEADERS)
        assert after.json()["status"] == "pending"


def test_rejecting_a_decomposition_that_created_nothing_unblocks_the_retry():
    """C4-R19a: `blocked` is a dead end — ``next_action`` answers NeedsHuman for
    it, so the rejection meant to invite another decomposition would prevent one."""
    with _materialization_client(status="blocked") as (c, _orch):
        r = c.post("/api/sdlc/initiatives/dark-mode/materialization/reject",
                   headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        detail = c.get("/api/sdlc/initiatives/dark-mode", headers=IDENTITY_HEADERS)
        assert detail.json()["status"] == "awaiting_human"


def test_rejecting_after_a_partial_apply_keeps_the_initiative_blocked():
    """Issues already exist on GitHub: re-decomposing would create a SECOND epic,
    so this one stays where the failed apply left it until a human sorts it out."""
    applied = {"repository_id": "R_1", "epic": {"node_id": "I_1", "number": 101},
               "tasks": {"0": {"node_id": "I_2", "number": 102}}}
    with _materialization_client(status="blocked", applied=applied) as (c, _orch):
        assert c.post("/api/sdlc/initiatives/dark-mode/materialization/reject",
                      headers=IDENTITY_HEADERS).status_code == 200
        detail = c.get("/api/sdlc/initiatives/dark-mode", headers=IDENTITY_HEADERS)
        assert detail.json()["status"] == "blocked"


def test_materialization_routes_404_for_an_unknown_initiative():
    with _materialization_client() as (c, _orch):
        assert c.get("/api/sdlc/initiatives/nope/materialization",
                     headers=IDENTITY_HEADERS).status_code == 404
        assert c.post("/api/sdlc/initiatives/nope/materialization/accept",
                      headers=IDENTITY_HEADERS).status_code == 404


def _seed_initiative(db_url: str, **fields):
    """One Initiative row before the client opens (its loop must not race ours)."""
    from server.db import db_session, init_db
    from server.db.models import Initiative

    async def _go():
        await init_db(db_url)
        async with db_session() as db:
            db.add(Initiative(**{"slug": "dark-mode", "repo": "o/r", "title": "Dark mode",
                                 "stage": "plan", "status": "active", **fields}))
    asyncio.run(_go())


def test_step_back_moves_the_stage_back_one_and_activates():
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        _seed_initiative(db_url, stage="build", status="awaiting_human")
        with _client(db_url) as c:
            r = c.post("/api/sdlc/initiatives/dark-mode/step-back", headers=IDENTITY_HEADERS)
            assert r.status_code == 200, r.text
            assert (r.json()["stage"], r.json()["status"]) == ("plan", "active")
            assert _audits(c, "sdlc.initiative.step_back") == [{"slug": "dark-mode", "stage": "plan"}]


def test_step_back_refuses_at_intake_and_when_blocked():
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        _seed_initiative(db_url, stage="intake")
        with _client(db_url) as c:
            r = c.post("/api/sdlc/initiatives/dark-mode/step-back", headers=IDENTITY_HEADERS)
            assert r.status_code == 409 and r.json()["detail"]["error"] == "at_first_stage"
            assert c.post("/api/sdlc/initiatives/nope/step-back", headers=IDENTITY_HEADERS).status_code == 404
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        _seed_initiative(db_url, stage="plan", status="blocked")
        with _client(db_url) as c:
            r = c.post("/api/sdlc/initiatives/dark-mode/step-back", headers=IDENTITY_HEADERS)
            assert r.status_code == 409 and r.json()["detail"]["error"] == "blocked"


def test_advance_refuses_a_blocked_initiative():
    """Advance was the accidental unblock: it flipped status to active AND moved
    the gate. A blocked row must be cleared on purpose, by the route for it."""
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        _seed_initiative(db_url, stage="plan", status="blocked")
        with _client(db_url) as c:
            r = c.post("/api/sdlc/initiatives/dark-mode/advance", headers=IDENTITY_HEADERS)
            assert r.status_code == 409, r.text
            assert r.json()["detail"]["error"] == "blocked"
            got = c.get("/api/sdlc/initiatives/dark-mode", headers=IDENTITY_HEADERS).json()
            assert (got["stage"], got["status"]) == ("plan", "blocked")


def test_unblock_returns_a_blocked_initiative_to_the_parked_state():
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        _seed_initiative(db_url, stage="plan", status="blocked")
        with _client(db_url) as c:
            r = c.post("/api/sdlc/initiatives/dark-mode/unblock", headers=IDENTITY_HEADERS)
            assert r.status_code == 200, r.text
            assert (r.json()["stage"], r.json()["status"]) == ("plan", "awaiting_human")
            assert _audits(c, "sdlc.initiative.unblock") == [{"slug": "dark-mode"}]
            # Not blocked any more → a second tap is a conflict, not a silent no-op.
            r2 = c.post("/api/sdlc/initiatives/dark-mode/unblock", headers=IDENTITY_HEADERS)
            assert r2.status_code == 409 and r2.json()["detail"]["error"] == "not_blocked"


def test_unblock_refuses_while_a_materialization_is_pending():
    """A pending proposal already has its two dispositions (accept resumes the
    apply, reject abandons it); unblocking underneath it would leave the card up
    on a row the loop thinks is free to decompose again."""
    from server.db import db_session, init_db
    from server.db.models import Initiative, MaterializationProposal

    async def _seed(db_url: str):
        await init_db(db_url)
        async with db_session() as db:
            ini = Initiative(slug="dark-mode", repo="o/r", title="Dark mode",
                             stage="plan", status="blocked")
            db.add(ini)
            await db.flush()
            db.add(MaterializationProposal(initiative_id=ini.id, status="pending",
                                           session_id="s", payload={"tasks": []}))

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        asyncio.run(_seed(db_url))
        with _client(db_url) as c:
            r = c.post("/api/sdlc/initiatives/dark-mode/unblock", headers=IDENTITY_HEADERS)
            assert r.status_code == 409, r.text
            assert r.json()["detail"]["error"] == "proposal_pending"


def _seed_exhausted(db_url: str, *, proposals: int, epic_number: int | None = None):
    """A plan-stage initiative the synthesizer blocked after N failed decompositions."""
    from server.db import db_session, init_db
    from server.db.models import Initiative, MaterializationProposal

    async def _run():
        await init_db(db_url)
        async with db_session() as db:
            ini = Initiative(slug="dark-mode", repo="o/r", title="Dark mode",
                             stage="plan", status="blocked", epic_repo="o/r" if epic_number else None,
                             epic_number=epic_number)
            db.add(ini)
            await db.flush()
            for _ in range(proposals):
                db.add(MaterializationProposal(initiative_id=ini.id, status="rejected",
                                               session_id="s", payload={"tasks": []}))

    asyncio.run(_run())


def test_unblock_refuses_an_initiative_the_synthesizer_gave_up_on():
    """The second writer of ``blocked`` is the synthesizer's exhaustion path, and
    clearing that block only re-blocks the row on the next tick."""
    from server.automations.sdlc_synthesizer import MATERIALIZE_MAX_ATTEMPTS

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        _seed_exhausted(db_url, proposals=MATERIALIZE_MAX_ATTEMPTS)
        with _client(db_url) as c:
            r = c.post("/api/sdlc/initiatives/dark-mode/unblock", headers=IDENTITY_HEADERS)
            assert r.status_code == 409, r.text
            assert r.json()["detail"]["error"] == "materialize_exhausted"
            assert str(MATERIALIZE_MAX_ATTEMPTS) in r.json()["detail"]["message"]
            got = c.get("/api/sdlc/initiatives/dark-mode", headers=IDENTITY_HEADERS).json()
            assert got["status"] == "blocked"


def test_unblock_allows_an_initiative_below_the_attempt_cap():
    """Under the cap the loop will decompose again, so the block IS clearable."""
    from server.automations.sdlc_synthesizer import MATERIALIZE_MAX_ATTEMPTS

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        _seed_exhausted(db_url, proposals=MATERIALIZE_MAX_ATTEMPTS - 1)
        with _client(db_url) as c:
            r = c.post("/api/sdlc/initiatives/dark-mode/unblock", headers=IDENTITY_HEADERS)
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "awaiting_human"


def test_unblock_ignores_the_attempt_cap_once_an_epic_exists():
    """The refusal is about a plan that never decomposed. An initiative that HAS
    an epic is past that writer — its block came from the failed apply."""
    from server.automations.sdlc_synthesizer import MATERIALIZE_MAX_ATTEMPTS

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        _seed_exhausted(db_url, proposals=MATERIALIZE_MAX_ATTEMPTS, epic_number=101)
        with _client(db_url) as c:
            r = c.post("/api/sdlc/initiatives/dark-mode/unblock", headers=IDENTITY_HEADERS)
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "awaiting_human"
