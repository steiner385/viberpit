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

    # Remote-sessiond vars are cleared globally in conftest.py
    # (_no_ambient_remote_daemon_config) so create_app() stays in-process.
    with contextlib.ExitStack() as stack:
        url = db_url
        if url is None:
            tmp = stack.enter_context(tempfile.TemporaryDirectory())
            url = f"sqlite+aiosqlite:///{tmp}/test.db"
        with patch.dict(os.environ, {"VIBERPIT_DB_URL": url,
                                     "VIBERPIT_DEV": "1",
                                     "VIBERPIT_TAILNET_ALLOWLIST": ""}):
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            app = create_app(orchestrator=orch)
            with TestClient(app, raise_server_exceptions=False) as c:
                yield c


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
                                triage_state="unclassified", hold_kind="owner_action",
                                hold_by="migration", hold_reason="test")
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


def test_leaves_carry_hold_fields():
    import asyncio
    from datetime import datetime, timezone

    from server.db import db_session, init_db
    from server.db.models import Feature, Project, WorkItem

    async def _seed(db_url: str) -> str:
        await init_db(db_url)
        async with db_session() as db:
            prod = Project(slug="example-product", name="Example product")
            db.add(prod)
            await db.flush()
            cap = Feature(project_id=prod.id, slug="dictation", title="Dictation", state="live")
            db.add(cap)
            await db.flush()
            held = WorkItem(repo="o/r", number=1, kind="issue", state="open", title="Held",
                            feature_id=cap.id, triage_state="unclassified",
                            hold_kind="owner_action", hold_by="migration", hold_reason="test",
                            hold_set_at=datetime.now(timezone.utc))
            plain = WorkItem(repo="o/r", number=2, kind="issue", state="open", title="Plain",
                             feature_id=cap.id, triage_state="classified",
                             reach=1, impact=1, effort=1, confidence=1)
            db.add_all([held, plain])
            await db.commit()
            return cap.id

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        cap_id = asyncio.run(_seed(db_url))

        with _client(db_url) as c:
            r = c.get(f"/api/pm/leaves?capability={cap_id}", headers=IDENTITY_HEADERS)
            assert r.status_code == 200, r.text
            by_ref = {i["ref"]: i for i in r.json()["items"]}
            assert by_ref["o/r#1"]["holdKind"] == "owner_action"
            assert by_ref["o/r#1"]["holdReason"] == "test"
            assert by_ref["o/r#1"]["holdBy"] == "migration"
            assert by_ref["o/r#1"]["holdSetAt"]
            assert by_ref["o/r#2"]["holdKind"] is None
            assert by_ref["o/r#2"]["holdReason"] is None
            assert by_ref["o/r#2"]["holdBy"] is None
            assert by_ref["o/r#2"]["holdSetAt"] is None


def test_leaves_show_not_needed_suppression_with_its_recheck_time():
    """#2683 review: an issue the queue skips as ``not_needed`` is not invisible — its
    leaf carries ``notNeededUntil`` (when it will be looked at again), its tier
    untouched: suppressed is not "needs you"."""
    import asyncio
    from datetime import datetime, timedelta, timezone

    from server.db import db_session, init_db
    from server.db.models import NOT_NEEDED_CAUSE, DispatchOutcome, Feature, Project, WorkItem
    from server.pm.outcomes import body_fingerprint, material_fingerprint

    ended = datetime.now(timezone.utc) - timedelta(hours=1)

    async def _seed(db_url: str) -> str:
        await init_db(db_url)
        async with db_session() as db:
            prod = Project(slug="example-product", name="Example product")
            db.add(prod)
            await db.flush()
            cap = Feature(project_id=prod.id, slug="dictation", title="Dictation", state="live")
            db.add(cap)
            await db.flush()
            db.add_all([
                WorkItem(repo="o/r", number=1, kind="issue", state="open", title="Judged",
                         body="b", labels=["ops"], feature_id=cap.id),
                WorkItem(repo="o/r", number=2, kind="issue", state="open", title="Plain",
                         body="b", feature_id=cap.id),
                DispatchOutcome(automation_id="backlog-runner", target_ref="o/r#1",
                                outcome="no_pr", cause=NOT_NEEDED_CAUSE,
                                started_at=ended - timedelta(hours=1), ended_at=ended,
                                body_fingerprint=body_fingerprint("Judged", "b"),
                                material_fingerprint=material_fingerprint(
                                    "Judged", "b", ["ops"])),
            ])
            await db.commit()
            return cap.id

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        cap_id = asyncio.run(_seed(db_url))

        with _client(db_url) as c:
            r = c.get(f"/api/pm/leaves?capability={cap_id}", headers=IDENTITY_HEADERS)
            assert r.status_code == 200, r.text
            by_ref = {i["ref"]: i for i in r.json()["items"]}
            until = datetime.fromisoformat(by_ref["o/r#1"]["notNeededUntil"])
            assert abs(until - (ended + timedelta(days=1))) < timedelta(seconds=1)
            assert by_ref["o/r#1"]["tier"] == by_ref["o/r#2"]["tier"]
            assert by_ref["o/r#2"]["notNeededUntil"] is None


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


def test_map_resolves_persona_slugs_onto_capability_nodes_live_only():
    import asyncio

    from server.db import db_session, init_db
    from server.db.models import Feature, Persona, PersonaFeature, Project

    async def _seed(db_url: str) -> None:
        await init_db(db_url)
        async with db_session() as db:
            prod = Project(slug="example-product", name="Example product")
            db.add(prod)
            await db.flush()
            sessions = Feature(project_id=prod.id, slug="sessions", title="Sessions", state="live")
            db.add(sessions)
            await db.flush()
            parent = Persona(project_id=prod.id, slug="parent", title="Parent")
            ghost = Persona(project_id=prod.id, slug="ghost", title="Ghost", orphaned=True)
            db.add_all([parent, ghost])
            await db.flush()
            db.add(PersonaFeature(persona_id=parent.id, feature_id=sessions.id))
            db.add(PersonaFeature(persona_id=ghost.id, feature_id=sessions.id))
            await db.commit()

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        asyncio.run(_seed(db_url))

        with _client(db_url) as c:
            r = c.get("/api/pm/map", headers=IDENTITY_HEADERS)
            assert r.status_code == 200, r.text
            body = r.json()
            products = {p["slug"]: p for p in body["products"]}
            roots = {cap["slug"]: cap for cap in products["example-product"]["capabilities"]}
            assert roots["sessions"]["personaSlugs"] == ["parent"]


def test_threshold_fallback_is_the_schema_default_for_both_handlers():
    """Both handlers read the D8 threshold through ONE helper, so an item with no
    owning product falls back to the POLICY SCHEMA's default rather than to a
    hand-written literal (the two defaulted to 0 and 2 before — R3-12)."""
    from server.api.pm_tree import _threshold_for
    from server.automations.product_policy import PRODUCT_POLICY_SCHEMA

    default = next(f.default for f in PRODUCT_POLICY_SCHEMA
                   if f.key == "give_up_after_bounces")
    assert _threshold_for(None) == default


def test_map_needs_you_definition_and_waiting_counters():
    """Design 2026-10-14 §1: a bare board Held card is waiting (tier blocked), an
    issue assigned to the product owner (a human ``trusted_authors`` login) needs
    you, and ``counts.waiting`` buckets every queue skip by reason — the SAME
    ``queue_inputs`` → ``rank`` composition ``GET /api/pm/queue`` uses. A product
    without a board reads all-zero waiting."""
    import asyncio

    from server.db import db_session, init_db
    from server.db.models import (Feature, GhProject, GhProjectItem, Project, ProjectRepo,
                                  WorkItem, WorkItemDep)

    async def _seed(db_url: str) -> str:
        await init_db(db_url)
        async with db_session() as db:
            prod = Project(slug="example-product", name="Example product",
                           automation_policy={"trusted_authors": ["Tony", "app/renovate"]})
            bare = Project(slug="no-board", name="No board")
            db.add_all([prod, bare])
            await db.flush()
            db.add(ProjectRepo(project_id=prod.id, repo="o/r", is_primary=True))
            feat = Feature(project_id=prod.id, slug="dictation", title="Dictation", state="live")
            db.add(feat)
            await db.flush()

            def issue(n: int, **kw) -> WorkItem:
                return WorkItem(repo="o/r", number=n, kind="issue", state="open", title=f"I{n}",
                                body="b", author="tony", feature_id=feat.id,
                                triage_state="classified", **kw)
            board_held = issue(1)
            mine = issue(2, assignees=["tony"])
            parked = issue(3, labels=["parked"])
            theirs = issue(4, assignees=["bob"])
            dep_blocked = issue(5)
            db.add_all([board_held, mine, parked, theirs, dep_blocked])
            await db.flush()
            db.add(WorkItemDep(src_work_item_id=dep_blocked.id, src_ref="o/r#5",
                               dst_ref="o/r#99", source="native", active=True))
            ghp = GhProject(project_id=prod.id, gh_project_number=1, title="example-product")
            db.add(ghp)
            await db.flush()
            for w, st in ((board_held, "Held"), (mine, "Ready"), (parked, "Ready"),
                          (theirs, "Ready"), (dep_blocked, "Ready")):
                db.add(GhProjectItem(gh_project_id=ghp.id, work_item_id=w.id, status=st))
            await db.commit()
            return feat.id

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        cap_id = asyncio.run(_seed(db_url))

        with _client(db_url) as c:
            r = c.get("/api/pm/map", headers=IDENTITY_HEADERS)
            assert r.status_code == 200, r.text
            products = {p["slug"]: p for p in r.json()["products"]}
            prod = products["example-product"]
            cap = prod["capabilities"][0]
            # Only the owner-assigned issue needs you; board-Held + dep-blocked wait.
            assert cap["counts"]["needsYou"] == 1
            assert cap["counts"]["blocked"] == 2
            waiting = prod["counts"]["waiting"]
            assert waiting["boardHeld"] == 1
            assert waiting["parked"] == 1
            assert waiting["assigned"] == 1          # bob's; the owner's is needs-you
            assert waiting["blocked"] == 1
            assert sum(waiting.values()) == 4
            assert set(products["no-board"]["counts"]["waiting"].values()) == {0}

            leaves = c.get(f"/api/pm/leaves?capability={cap_id}", headers=IDENTITY_HEADERS)
            assert leaves.status_code == 200, leaves.text
            tiers = {leaf["number"]: leaf["tier"] for leaf in leaves.json()["items"]}
            assert tiers[1] == "blocked"
            assert tiers[2] == "needs-you"
            assert tiers[4] != "needs-you"
