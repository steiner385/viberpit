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


def test_map_carries_the_open_map_changes_pointer_per_product():
    """Spec §9: the product header shows the open map-changes PR, its age and
    whether it is behind — so the map payload carries the pointer per product,
    and ``null`` for a product without one. Only an ``open`` row counts (R8b-5),
    and the newest one wins."""
    import asyncio
    from datetime import datetime, timezone

    from server.db import db_session, init_db
    from server.db.models import FeatureMapChange, Project, ProjectRepo

    async def _seed(db_url: str) -> None:
        await init_db(db_url)
        async with db_session() as db:
            prod = Project(slug="example-product", name="Example product")
            other = Project(slug="example-infra", name="Infra")
            db.add_all([prod, other])
            await db.flush()
            db.add(ProjectRepo(project_id=prod.id, repo="o/r", is_primary=True))
            db.add(FeatureMapChange(
                project_id=prod.id, repo="o/r", branch="pm/map-changes-example-product",
                pr_number=12, pr_url="https://github.com/o/r/pull/12", status="open",
                head_sha="c0", behind=True, conflicting=False,
                opened_at=datetime(2026, 9, 10, tzinfo=timezone.utc)))
            # An older abandoned row on the same product must not be picked up…
            db.add(FeatureMapChange(
                project_id=prod.id, repo="o/r", branch="pm/map-changes-example-product",
                pr_number=9, status="abandoned",
                opened_at=datetime(2026, 9, 1, tzinfo=timezone.utc)))
            # …and a product with no row at all reports null.
            await db.commit()

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        asyncio.run(_seed(db_url))

        with _client(db_url) as c:
            r = c.get("/api/pm/map", headers=IDENTITY_HEADERS)
            assert r.status_code == 200, r.text
            products = {p["slug"]: p for p in r.json()["products"]}
            assert products["example-product"]["mapChanges"] == {
                "branch": "pm/map-changes-example-product",
                "prNumber": 12,
                "prUrl": "https://github.com/o/r/pull/12",
                "status": "open",
                "openedAt": "2026-09-10T00:00:00+00:00",
                "headSha": "c0",
                "behind": True,
                "conflicting": False,
            }
            assert products["example-infra"]["mapChanges"] is None


def test_board_status_blocked_tiers_the_issue_on_leaves_and_map():
    """Spec §11 board-Status tier row: an otherwise-idle open issue mirrored onto
    a GhProject board with Status="Blocked" gets tier "blocked" — via /leaves for
    the issue itself, and rolled up into the capability's counts["blocked"] via
    /map — even with no native blocker edge and no live session."""
    import asyncio

    from server.db import db_session, init_db
    from server.db.models import Feature, GhProject, GhProjectItem, Project, ProjectRepo, WorkItem

    async def _seed(db_url: str) -> str:
        await init_db(db_url)
        async with db_session() as db:
            prod = Project(slug="example-product", name="Example product")
            db.add(prod)
            await db.flush()
            db.add(ProjectRepo(project_id=prod.id, repo="o/r", is_primary=True))
            dictation = Feature(project_id=prod.id, slug="dictation", title="Dictation", state="live")
            db.add(dictation)
            await db.flush()
            issue = WorkItem(repo="o/r", number=1, kind="issue", state="open", title="Idle-looking",
                             feature_id=dictation.id, triage_state="classified")
            db.add(issue)
            await db.flush()
            ghp = GhProject(project_id=prod.id, gh_project_number=1, title="example-product")
            db.add(ghp)
            await db.flush()
            db.add(GhProjectItem(gh_project_id=ghp.id, work_item_id=issue.id, status="Blocked"))
            await db.commit()
            return dictation.id

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        cap_id = asyncio.run(_seed(db_url))

        with _client(db_url) as c:
            leaves = c.get(f"/api/pm/leaves?capability={cap_id}", headers=IDENTITY_HEADERS)
            assert leaves.status_code == 200, leaves.text
            items = leaves.json()["items"]
            assert len(items) == 1 and items[0]["tier"] == "blocked"

            r = c.get("/api/pm/map", headers=IDENTITY_HEADERS)
            assert r.status_code == 200, r.text
            products = {p["slug"]: p for p in r.json()["products"]}
            dictation = products["example-product"]["capabilities"][0]
            assert dictation["slug"] == "dictation"
            assert dictation["counts"]["blocked"] == 1
            assert dictation["tier"] == "blocked"


def test_leaves_for_capability_and_initiative():
    import asyncio

    from server.db import db_session, init_db
    from server.db.models import Feature, Initiative, Project, WorkItem

    async def _seed(db_url: str) -> str:
        await init_db(db_url)
        async with db_session() as db:
            prod = Project(slug="example-product", name="Example product")
            db.add(prod)
            await db.flush()
            cap = Feature(project_id=prod.id, slug="dictation", title="Dictation", state="live")
            db.add(cap)
            await db.flush()
            epic = WorkItem(repo="o/r", number=10, kind="issue", state="open", title="Epic",
                             is_epic=True)
            db.add(epic)
            await db.flush()
            low = WorkItem(repo="o/r", number=1, kind="issue", state="open", title="Low",
                           feature_id=cap.id, triage_state="classified",
                           reach=1, impact=1, effort=1, confidence=1)
            high = WorkItem(repo="o/r", number=2, kind="issue", state="open", title="High",
                            feature_id=cap.id, triage_state="classified",
                            reach=5, impact=5, effort=1, confidence=5, parent_work_item_id=epic.id)
            done = WorkItem(repo="o/r", number=3, kind="issue", state="closed", title="Done",
                            feature_id=cap.id, triage_state="classified",
                            reach=9, impact=9, effort=1, confidence=9, parent_work_item_id=epic.id)
            db.add_all([low, high, done])
            ini = Initiative(slug="whisper", repo="o/r", title="W", stage="build", status="active",
                             project_id=prod.id, epic_repo="o/r", epic_number=10)
            db.add(ini)
            await db.commit()
            return cap.id

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        cap_id = asyncio.run(_seed(db_url))

        with _client(db_url) as c:
            r = c.get(f"/api/pm/leaves?capability={cap_id}", headers=IDENTITY_HEADERS)
            assert r.status_code == 200, r.text
            items = r.json()["items"]
            assert [i["number"] for i in items] == [2, 1, 3]          # open by score desc, closed last
            assert items[0]["score"] == 125.0 and items[0]["tier"] == "idle"
            assert items[0]["parentRef"] == "o/r#10"
            assert items[0]["featureId"] == cap_id
            assert items[2]["tier"] == "live"

            r = c.get("/api/pm/leaves?initiative=whisper", headers=IDENTITY_HEADERS)
            assert [i["number"] for i in r.json()["items"]] == [2, 3]  # the epic's children only

            assert c.get("/api/pm/leaves", headers=IDENTITY_HEADERS).status_code == 422
            assert c.get(f"/api/pm/leaves?capability={cap_id}&initiative=whisper",
                         headers=IDENTITY_HEADERS).status_code == 422
            assert c.get("/api/pm/leaves?capability=nope", headers=IDENTITY_HEADERS).status_code == 404
            assert c.get("/api/pm/leaves?initiative=nope", headers=IDENTITY_HEADERS).status_code == 404


def test_leaves_and_map_surface_needs_spec_bounces():
    """D8: a product's `give_up_after_bounces` policy turns FINALISED non-merged
    outcomes at an issue's CURRENT body into a `needs_spec` reason — on
    /api/pm/leaves (tier/reason/bounces on the leaf) and rolled into /api/pm/map's
    needsYou count. An outcome row for a STALE fingerprint (the body has since
    been edited) never counts (spec §7/§8, D8; constitution VI: pm_tree.py never
    re-compares the count itself)."""
    import asyncio

    from server.db import db_session, init_db
    from server.db.models import DispatchOutcome, Feature, Project, ProjectRepo, WorkItem
    from server.pm.outcomes import body_fingerprint

    async def _seed(db_url: str) -> str:
        await init_db(db_url)
        async with db_session() as db:
            prod = Project(slug="example-product", name="Example product",
                           automation_policy={"give_up_after_bounces": 2})
            db.add(prod)
            await db.flush()
            db.add(ProjectRepo(project_id=prod.id, repo="o/r", is_primary=True))
            cap = Feature(project_id=prod.id, slug="dictation", title="Dictation", state="live")
            db.add(cap)
            await db.flush()
            bounced = WorkItem(repo="o/r", number=1, kind="issue", state="open", title="Bounced",
                               body="needs a spec", feature_id=cap.id, triage_state="classified")
            stale = WorkItem(repo="o/r", number=2, kind="issue", state="open", title="Stale",
                             body="edited since", feature_id=cap.id, triage_state="classified")
            # Bounced the same number of times, but DONE: a resolved issue carries no
            # reason (R3-12) — its tier already reads 'live'.
            done = WorkItem(repo="o/r", number=3, kind="issue", state="closed", title="Done",
                            body="shipped anyway", feature_id=cap.id, triage_state="classified")
            db.add_all([bounced, stale, done])
            await db.flush()
            fp = body_fingerprint("Bounced", "needs a spec")
            done_fp = body_fingerprint("Done", "shipped anyway")
            db.add_all([
                DispatchOutcome(target_ref="o/r#1", body_fingerprint=fp, outcome="no_pr"),
                DispatchOutcome(target_ref="o/r#1", body_fingerprint=fp, outcome="no_pr"),
                # A row for #2's OLD body — must not count against the current one.
                DispatchOutcome(target_ref="o/r#2", body_fingerprint="stale-fp", outcome="no_pr"),
                DispatchOutcome(target_ref="o/r#3", body_fingerprint=done_fp, outcome="no_pr"),
                DispatchOutcome(target_ref="o/r#3", body_fingerprint=done_fp, outcome="no_pr"),
            ])
            await db.commit()
            return cap.id

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        cap_id = asyncio.run(_seed(db_url))

        with _client(db_url) as c:
            leaves = c.get(f"/api/pm/leaves?capability={cap_id}", headers=IDENTITY_HEADERS)
            assert leaves.status_code == 200, leaves.text
            by_ref = {i["ref"]: i for i in leaves.json()["items"]}
            assert by_ref["o/r#1"]["tier"] == "needs-you"
            assert by_ref["o/r#1"]["reason"] == "needs_spec"
            assert by_ref["o/r#1"]["bounces"] == 2
            assert by_ref["o/r#2"]["reason"] is None
            assert by_ref["o/r#2"]["bounces"] == 0
            assert by_ref["o/r#2"]["tier"] == "idle"
            # The closed leaf keeps its bounce COUNT (the history is real) but the
            # reason is scrubbed, so the chip cannot contradict the 'live' tier.
            assert by_ref["o/r#3"]["tier"] == "live"
            assert by_ref["o/r#3"]["reason"] is None
            assert by_ref["o/r#3"]["bounces"] == 2

            r = c.get("/api/pm/map", headers=IDENTITY_HEADERS)
            assert r.status_code == 200, r.text
            products = {p["slug"]: p for p in r.json()["products"]}
            dictation = products["example-product"]["capabilities"][0]
            assert dictation["slug"] == "dictation"
            assert dictation["counts"]["needsYou"] == 1


def test_threshold_fallback_is_the_schema_default_for_both_handlers():
    """Both handlers read the D8 threshold through ONE helper, so an item with no
    owning product falls back to the POLICY SCHEMA's default rather than to a
    hand-written literal (the two defaulted to 0 and 2 before — R3-12)."""
    from server.api.pm_tree import _threshold_for
    from server.automations.product_policy import PRODUCT_POLICY_SCHEMA

    default = next(f.default for f in PRODUCT_POLICY_SCHEMA
                   if f.key == "give_up_after_bounces")
    assert _threshold_for(None) == default
