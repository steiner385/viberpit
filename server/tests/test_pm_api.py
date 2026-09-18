"""PM registry API tests (spec §7, phase-1 routes).

One narrated flow (create → list → sync-now → edges) plus edge cases, in the
style of test_sdlc_api.py — reuse that module's fixtures.
"""
from __future__ import annotations

from server.tests.pm_ownership_fixtures import identity_repositories, ownership_snapshot
import pytest





import os
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

from server.config import Settings

@pytest.fixture(autouse=True)
def canonical_mutation_repositories(monkeypatch):
    monkeypatch.setattr("server.automations.target_identity.resolve_repositories", identity_repositories)


IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}


@contextmanager
def _client():
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    # Unset remote-sessiond vars so create_app() stays in-process (no TLS needed).
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


def test_project_lifecycle():
    """Narrated create → list → sync-now → 404 flow."""
    with _client() as c:
        # create
        resp = c.post("/api/pm/projects", json={
            "name": "KinDash",
            "repos": [{"repo": "steiner385/KinDash", "isPrimary": True}],
        }, headers=IDENTITY_HEADERS)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["slug"] == "kindash"
        assert body["onboardingState"] == "registered"
        # create must return the full Project shape the frontend type requires
        assert body["repos"][0]["repo"] == "steiner385/KinDash"
        assert body["edges"] == []
        assert body["featureCount"] == 0
        assert body["workItemCount"] == 0

        # duplicate slug conflicts
        resp = c.post("/api/pm/projects", json={
            "name": "KinDash", "repos": [{"repo": "x/y"}]},
            headers=IDENTITY_HEADERS)
        assert resp.status_code == 409

        # empty repos rejected
        resp = c.post("/api/pm/projects", json={"name": "Norepo", "repos": []},
                      headers=IDENTITY_HEADERS)
        assert resp.status_code == 422

        # malformed owner/repo rejected — the GraphQL builders %-interpolate these
        # strings, so a quote/space/missing-slash must never reach persistence
        # (issue #973). Each is a distinct 422 with the invalid_repo error code.
        for bad in ('steiner385/Kin"Dash', "steiner385 / KinDash",
                    "no-slash", "steiner385/", "a/b/c"):
            resp = c.post("/api/pm/projects", json={
                "name": "Bad", "repos": [{"repo": bad}]}, headers=IDENTITY_HEADERS)
            assert resp.status_code == 422, f"{bad!r} should be rejected: {resp.text}"
            assert resp.json()["detail"]["error"] == "invalid_repo"

        # list carries repos + counts
        resp = c.get("/api/pm/projects", headers=IDENTITY_HEADERS)
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        proj = items[0]
        assert proj["repos"][0]["repo"] == "steiner385/KinDash"
        assert proj["workItemCount"] == 0
        assert proj["featureCount"] == 0

        # sync-now stamps the request
        resp = c.post("/api/pm/projects/kindash/sync", headers=IDENTITY_HEADERS)
        assert resp.status_code == 202
        assert resp.json() == {"requested": True}

        # detail 404
        resp = c.get("/api/pm/projects/nope", headers=IDENTITY_HEADERS)
        assert resp.status_code == 404


def test_update_project_renames_name_slug_and_repoints_repo():
    """PATCH /projects/{slug}: name-only, slug rename (old slug 404s, new works),
    then a primary-repo repoint that resets backfill state."""
    with _client() as c:
        resp = c.post("/api/pm/projects", json={
            "name": "LakotaGrades",
            "repos": [{"repo": "steiner385/lakota-grades-mcp", "isPrimary": True}],
        }, headers=IDENTITY_HEADERS)
        assert resp.status_code == 201, resp.text

        # name only
        resp = c.patch("/api/pm/projects/lakotagrades", json={"name": "FridgeSheet"},
                       headers=IDENTITY_HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["name"] == "FridgeSheet"
        assert body["slug"] == "lakotagrades"

        # slug rename
        resp = c.patch("/api/pm/projects/lakotagrades", json={"slug": "fridgesheet"},
                       headers=IDENTITY_HEADERS)
        assert resp.status_code == 200, resp.text
        assert resp.json()["slug"] == "fridgesheet"
        resp = c.get("/api/pm/projects/lakotagrades", headers=IDENTITY_HEADERS)
        assert resp.status_code == 404
        resp = c.get("/api/pm/projects/fridgesheet", headers=IDENTITY_HEADERS)
        assert resp.status_code == 200

        # repo repoint — resets backfill/sync bookkeeping on the primary row
        resp = c.patch("/api/pm/projects/fridgesheet", json={"repo": "steiner385/fridgesheet"},
                       headers=IDENTITY_HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["repos"][0]["repo"] == "steiner385/fridgesheet"
        assert body["repos"][0]["isPrimary"] is True
        assert body["repos"][0]["backfillDone"] is False
        assert body["repos"][0]["lastSyncedAt"] is None

        # repointing to the SAME repo again is a no-op, not an ownership conflict
        resp = c.patch("/api/pm/projects/fridgesheet", json={"repo": "steiner385/fridgesheet"},
                       headers=IDENTITY_HEADERS)
        assert resp.status_code == 200, resp.text

        # an empty patch (no fields) is a 200 no-op, not a 422
        resp = c.patch("/api/pm/projects/fridgesheet", json={}, headers=IDENTITY_HEADERS)
        assert resp.status_code == 200, resp.text


def test_update_project_rejects_bad_input_and_conflicts():
    with _client() as c:
        for name in ("Alpha", "Beta"):
            resp = c.post("/api/pm/projects", json={
                "name": name, "repos": [{"repo": f"o/{name.lower()}"}]},
                headers=IDENTITY_HEADERS)
            assert resp.status_code == 201, resp.text

        # blank name
        resp = c.patch("/api/pm/projects/alpha", json={"name": "  "}, headers=IDENTITY_HEADERS)
        assert resp.status_code == 422
        assert resp.json()["detail"]["error"] == "bad_name"

        # non-branch-safe slug (uppercase, spaces — the same SLUG_RE map-changes
        # branch names must satisfy)
        resp = c.patch("/api/pm/projects/alpha", json={"slug": "Not Safe"}, headers=IDENTITY_HEADERS)
        assert resp.status_code == 422
        assert resp.json()["detail"]["error"] == "bad_slug"

        # slug already taken by another project
        resp = c.patch("/api/pm/projects/alpha", json={"slug": "beta"}, headers=IDENTITY_HEADERS)
        assert resp.status_code == 409
        assert resp.json()["detail"]["error"] == "slug_conflict"

        # malformed repo
        resp = c.patch("/api/pm/projects/alpha", json={"repo": "no-slash"}, headers=IDENTITY_HEADERS)
        assert resp.status_code == 422
        assert resp.json()["detail"]["error"] == "invalid_repo"

        # repo already registered to another project
        resp = c.patch("/api/pm/projects/alpha", json={"repo": "o/beta"}, headers=IDENTITY_HEADERS)
        assert resp.status_code == 409
        assert resp.json()["detail"]["error"] == "ownership_conflict"

        # unknown project
        resp = c.patch("/api/pm/projects/nope", json={"name": "X"}, headers=IDENTITY_HEADERS)
        assert resp.status_code == 404


def test_update_project_refuses_slug_and_repo_changes_while_map_changes_is_open():
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import FeatureMapChange, Project

    with _client() as c:
        resp = c.post("/api/pm/projects", json={
            "name": "Gamma", "repos": [{"repo": "o/gamma", "isPrimary": True}]},
            headers=IDENTITY_HEADERS)
        assert resp.status_code == 201, resp.text

        async def _open_pointer():
            async with db_session() as db:
                p = (await db.execute(select(Project).where(
                    Project.slug == "gamma"))).scalars().one()
                db.add(FeatureMapChange(
                    project_id=p.id, repo="o/gamma", branch="pm/map-changes-gamma",
                    pr_number=1, pr_url="https://github.com/o/gamma/pull/1", status="open"))
                await db.commit()
        c.portal.call(_open_pointer)

        resp = c.patch("/api/pm/projects/gamma", json={"slug": "delta"}, headers=IDENTITY_HEADERS)
        assert resp.status_code == 409
        assert resp.json()["detail"]["error"] == "open_map_changes"

        resp = c.patch("/api/pm/projects/gamma", json={"repo": "o/gamma2"}, headers=IDENTITY_HEADERS)
        assert resp.status_code == 409
        assert resp.json()["detail"]["error"] == "open_map_changes"

        # a name-only patch is unaffected — it does not touch the branch/repo
        resp = c.patch("/api/pm/projects/gamma", json={"name": "Gamma Two"}, headers=IDENTITY_HEADERS)
        assert resp.status_code == 200, resp.text


def test_policy_schema_and_project_policy_round_trip():
    with _client() as c:
        schema = c.get("/api/pm/policy-schema", headers=IDENTITY_HEADERS)
        assert schema.status_code == 200
        fields = {f["key"]: f for f in schema.json()}
        assert list(fields)[:2] == ["dispatch", "queue_min_status"]
        # The int fields publish their floor so the browser renders the same range
        # the server enforces, instead of hard-coding one (or none).
        assert fields["max_in_flight"]["min"] == 0      # 0 = paused, not unlimited
        assert fields["weight"]["min"] == 0
        # ``max_attempts`` is retired (plan 4a task 2): the ladder climbs on a
        # bounce instead of counting attempts against a flat cap.
        assert fields["ladder_top"]["choices"][-1] == "fable"
        assert fields["ladder_top"]["default"] == "fable"
        assert fields["dispatch"]["min"] is None
        assert fields["risk_tolerance"]["choices"] == ["cautious", "balanced", "bold"]
        assert fields["max_initiatives_in_flight"]["min"] == 0
        assert fields["unattended_model_floor"]["default"] == "none"

        r = c.post("/api/pm/projects", json={"name": "KinDash", "slug": "kindash",
                                             "repos": [{"repo": "cairnea/KinDash", "isPrimary": True}]},
                   headers=IDENTITY_HEADERS)
        assert r.status_code == 201, r.text
        assert r.json()["automationPolicy"] is None

        r = c.patch("/api/pm/projects/kindash/policy",
                    json={"policy": {"dispatch": "auto", "max_in_flight": 2}}, headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        pol = r.json()["automationPolicy"]
        assert pol["dispatch"] == "auto" and pol["max_in_flight"] == 2
        assert "queue_min_status" not in pol  # defaults are display/effective only
        assert c.get("/api/pm/projects/kindash", headers=IDENTITY_HEADERS).json()["automationPolicy"] == pol

        r = c.patch("/api/pm/projects/kindash/policy", json={"policy": {"dispatch": "turbo"}},
                    headers=IDENTITY_HEADERS)
        assert r.status_code == 422 and r.json()["detail"]["error"] == "bad_policy"
        r = c.patch("/api/pm/projects/kindash/policy", json={"policy": {"nope": 1}}, headers=IDENTITY_HEADERS)
        assert r.status_code == 422
        # A below-minimum int is a server-side refusal, not a browser courtesy: a
        # stored ``-1`` cap reads as an unreachable ceiling in ``_refill``, i.e.
        # unlimited spawning for that product.
        r = c.patch("/api/pm/projects/kindash/policy",
                    json={"policy": {"dispatch": "auto", "max_in_flight": -1}},
                    headers=IDENTITY_HEADERS)
        assert r.status_code == 422 and r.json()["detail"]["error"] == "bad_policy"
        assert "minimum" in r.json()["detail"]["message"]
        # …while 0 is a legal, meaningful value (paused).
        r = c.patch("/api/pm/projects/kindash/policy",
                    json={"policy": {"dispatch": "auto", "max_in_flight": 0}},
                    headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        paused = r.json()["automationPolicy"]
        assert paused["max_in_flight"] == 0

        r = c.patch("/api/pm/projects/kindash/policy", json={"policy": None}, headers=IDENTITY_HEADERS)
        assert r.status_code == 200 and r.json()["automationPolicy"] is None
        assert c.patch("/api/pm/projects/nope/policy", json={"policy": {}},
                       headers=IDENTITY_HEADERS).status_code == 404

        # Every policy write is AUDITED with the acting identity and the validated
        # blob — who told the runner to start (or stop) working a product, and
        # what it was told, is the record an operator reconstructs a spawn from.
        # No GET /api/audit route exists, so read the rows the DB-side way
        # (the idiom in test_triage_undo_writes_audit_row).
        from sqlalchemy import select

        from server.db import db_session
        from server.db.models import AuditEvent

        async def _audits():
            async with db_session() as db:
                return (await db.execute(select(AuditEvent).where(
                    AuditEvent.type == "pm.project.policy"))).scalars().all()
        found = c.portal.call(_audits)
        # Three accepted writes: the auto/2 policy, the paused (0) one and the
        # clear. The four rejected ones (bad enum, unknown key, below-minimum int,
        # unknown slug) write nothing.
        assert [a.detail["policy"] for a in found] == [pol, paused, None]
        assert {a.detail["slug"] for a in found} == {"kindash"}
        assert {a.identity for a in found} == {IDENTITY_HEADERS["Tailscale-User-Login"]}


def test_edges():
    """Create two projects, link with an edge, verify bad-kind/bad-dst 422/404, delete."""
    with _client() as c:
        for name in ("Alpha", "Beta"):
            resp = c.post("/api/pm/projects", json={
                "name": name, "repos": [{"repo": f"o/{name.lower()}"}]},
                headers=IDENTITY_HEADERS)
            assert resp.status_code == 201, resp.text

        resp = c.post("/api/pm/projects/alpha/edges", json={
            "dstSlug": "beta", "kind": "depends_on"},
            headers=IDENTITY_HEADERS)
        assert resp.status_code == 201, resp.text
        edge_id = resp.json()["id"]

        # bad kind → 422
        resp = c.post("/api/pm/projects/alpha/edges", json={
            "dstSlug": "beta", "kind": "bogus"},
            headers=IDENTITY_HEADERS)
        assert resp.status_code == 422

        # unknown dst → 404
        resp = c.post("/api/pm/projects/alpha/edges", json={
            "dstSlug": "ghost", "kind": "depends_on"},
            headers=IDENTITY_HEADERS)
        assert resp.status_code == 404

        # delete edge → 204
        resp = c.delete(f"/api/pm/projects/alpha/edges/{edge_id}",
                        headers=IDENTITY_HEADERS)
        assert resp.status_code == 204


# ---------------------------------------------------------------------------
# Phase-2 tests: onboarding wizard + propose-map/classify + triage
# ---------------------------------------------------------------------------



@contextmanager
def _client_with_stubbed_seams():
    """TestClient whose orchestrator PM seams are recorder stubs (no real
    sessions), mirroring test_pm_api._client but replacing the spawn seams."""
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

                async def _fake_spawn_map(slug, **_kwargs):
                    return type("R", (), {"id": "sess-map"})()

                async def _fake_classify_run(slug, **_kwargs):
                    return 3

                orch.pm_onboard_spawn_map = _fake_spawn_map   # type: ignore[method-assign]
                orch.pm_classify_run = _fake_classify_run     # type: ignore[method-assign]
                app = create_app(orchestrator=orch)
                with TestClient(app, raise_server_exceptions=False) as c:
                    yield c
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


def _register(c, name, repo="o/r", primary=True):
    r = c.post("/api/pm/projects", json={
        "name": name, "repos": [{"repo": repo, "isPrimary": primary}]},
        headers=IDENTITY_HEADERS)
    assert r.status_code == 201, r.text
    return r.json()


def _seed_map_merged(c, slug):
    """Drive a project to map_merged with a feature + open unclassified items.

    Seeded through the app's OWN event loop (``c.portal``), like every other DB
    access in this module — see the note on :func:`_seed_one_pending`.
    """
    from server.db import db_session
    from sqlalchemy import select
    from server.db.models import Feature, Project, ProjectRepo, WorkItem

    async def _run():
        async with db_session() as db:
            p = (await db.execute(select(Project).where(
                Project.slug == slug))).scalar_one()
            p.onboarding_state = "map_merged"
            (await db.execute(select(ProjectRepo).where(
                ProjectRepo.project_id == p.id))).scalar_one().backfill_done = True
            db.add(Feature(project_id=p.id, slug="sessions", title="Sessions",
                           state="live", summary="Session mgr."))
            db.add(WorkItem(repo="o/r", number=1, kind="issue", title="x",
                            state="open", triage_state="unclassified"))
    c.portal.call(_run)


def test_onboarding_state_reflects_engine():
    with _client_with_stubbed_seams() as c:
        _register(c, "Viberpit", repo="o/r")
        r = c.get("/api/pm/projects/viberpit/onboarding", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        body = r.json()
        # Registered project with a repo but no backfill → await_backfill.
        assert body["state"] == "registered"
        assert body["action"]["type"] == "await_backfill"
        assert body["counts"]["repoCount"] == 1


def test_propose_map_conflicts_when_backfill_incomplete():
    with _client_with_stubbed_seams() as c:
        _register(c, "Viberpit", repo="o/r")
        r = c.post("/api/pm/projects/viberpit/propose-map", headers=IDENTITY_HEADERS)
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "backfill_incomplete"


def test_classify_409_when_not_eligible():
    """Classify guard: 409 with error=map_not_merged when state is 'registered'."""
    with _client_with_stubbed_seams() as c:
        _register(c, "Viberpit", repo="o/r")
        r = c.post("/api/pm/projects/viberpit/classify", headers=IDENTITY_HEADERS)
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "map_not_merged"


def test_advance_map_transitions_state():
    """advance-map: 200 in map_proposed → onboardingState becomes map_merged;
    409 when state is wrong; classify becomes available after."""
    from server.db import db_session
    from sqlalchemy import select
    from server.db.models import Project, ProjectRepo

    with _client_with_stubbed_seams() as c:
        _register(c, "Viberpit", repo="o/r")

        # Must be in map_proposed — 409 when still registered
        r = c.post("/api/pm/projects/viberpit/advance-map", headers=IDENTITY_HEADERS)
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "not_map_proposed"

        # Force state to map_proposed + backfill_done
        async def _set_proposed():
            async with db_session() as db:
                p = (await db.execute(select(Project).where(
                    Project.slug == "viberpit"))).scalar_one()
                p.onboarding_state = "map_proposed"
                (await db.execute(select(ProjectRepo).where(
                    ProjectRepo.project_id == p.id))).scalar_one().backfill_done = True
        c.portal.call(_set_proposed)

        # advance succeeds
        r = c.post("/api/pm/projects/viberpit/advance-map", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["onboardingState"] == "map_merged"

        # DB reflects the new state
        async def _check():
            async with db_session() as db:
                p = (await db.execute(select(Project).where(
                    Project.slug == "viberpit"))).scalar_one()
                assert p.onboarding_state == "map_merged"
        c.portal.call(_check)


def test_classify_dispatches_and_is_idempotent():
    # Full narrated flow: register → force map_merged → classify → accept.
    with _client_with_stubbed_seams() as c:
        _register(c, "Viberpit", repo="o/r")
        _seed_map_merged(c, "viberpit")

        r = c.post("/api/pm/projects/viberpit/classify", headers=IDENTITY_HEADERS)
        assert r.status_code == 202, r.text
        assert r.json()["dispatched"] == 3


def test_triage_accept_and_reject_flow():
    from server.db import db_session
    from sqlalchemy import select
    from server.db.models import (
        Feature, Project, TriageProposal, WorkItem)

    with _client_with_stubbed_seams() as c:
        _register(c, "Viberpit", repo="o/r")

        async def _seed_props():
            async with db_session() as db:
                p = (await db.execute(select(Project).where(
                    Project.slug == "viberpit"))).scalar_one()
                f = Feature(project_id=p.id, slug="sessions", title="Sessions",
                            state="live")
                w1 = WorkItem(repo="o/r", number=1, kind="issue", title="a",
                              state="open", triage_state="proposed")
                w2 = WorkItem(repo="o/r", number=2, kind="issue", title="b",
                              state="open", triage_state="proposed")
                db.add_all([f, w1, w2])
                await db.flush()
                db.add(TriageProposal(work_item_id=w1.id, proposed_feature_id=f.id,
                                      reach=3, impact=2, effort=1, confidence=2,
                                      rationale="fits", confidence_level="high",
                                      status="pending"))
                db.add(TriageProposal(work_item_id=w2.id, no_fit_suggestion="billing",
                                      rationale="no fit", confidence_level="low",
                                      status="pending"))
        c.portal.call(_seed_props)

        listed = c.get("/api/pm/triage", headers=IDENTITY_HEADERS)
        assert listed.status_code == 200, listed.text
        items = listed.json()
        assert len(items) == 2
        accept_id = next(i["id"] for i in items if i["proposedFeatureSlug"] == "sessions")
        reject_id = next(i["id"] for i in items if i["noFitSuggestion"] == "billing")

        # accept copies onto the overlay
        r = c.post(f"/api/pm/triage/{accept_id}/accept", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text

        # reject parks the item as an owner-action hold by the rejecting person
        r = c.post(f"/api/pm/triage/{reject_id}/reject", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        rejected_wid = r.json()["workItemId"]
        # no GET /api/pm/work-items/{id} route exists — read the item back through
        # the overlay's empty-PATCH read-through instead.
        wi = c.patch(f"/api/pm/work-items/{rejected_wid}/overlay", json={},
                     headers=IDENTITY_HEADERS)
        assert wi.status_code == 200, wi.text
        assert wi.json()["holdKind"] == "owner_action"
        assert wi.json()["holdBy"] == "testuser@example.com"

        # double-accept the already-resolved proposal → 409
        r = c.post(f"/api/pm/triage/{accept_id}/accept", headers=IDENTITY_HEADERS)
        assert r.status_code == 409

        async def _check():
            async with db_session() as db:
                w1 = (await db.execute(select(WorkItem).where(
                    WorkItem.number == 1))).scalar_one()
                w2 = (await db.execute(select(WorkItem).where(
                    WorkItem.number == 2))).scalar_one()
                assert w1.triage_state == "classified"
                assert w1.feature_id is not None and w1.impact == 2
                # classified_by is the constant "human", NOT the tailnet identity.
                assert w1.classified_by == "human"
                assert w2.hold_kind == "owner_action"
                assert w2.hold_by == "testuser@example.com"
        c.portal.call(_check)


def test_triage_accept_applies_edits_body():
    """An optional {edits} body overrides copied overlay values (the shared
    apply_accept helper); this is what phase-3 edit-then-accept relies on."""
    from server.db import db_session
    from sqlalchemy import select
    from server.db.models import (
        Feature, Project, TriageProposal, WorkItem)

    with _client_with_stubbed_seams() as c:
        _register(c, "Viberpit", repo="o/r")

        async def _seed_one():
            async with db_session() as db:
                p = (await db.execute(select(Project).where(
                    Project.slug == "viberpit"))).scalar_one()
                f = Feature(project_id=p.id, slug="sessions", title="Sessions",
                            state="live")
                w = WorkItem(repo="o/r", number=1, kind="issue", title="a",
                             state="open", triage_state="proposed")
                db.add_all([f, w])
                await db.flush()
                db.add(TriageProposal(work_item_id=w.id, proposed_feature_id=f.id,
                                      reach=3, impact=2, effort=1, confidence=2,
                                      rationale="fits", confidence_level="high",
                                      status="pending"))
        c.portal.call(_seed_one)

        listed = c.get("/api/pm/triage", headers=IDENTITY_HEADERS).json()
        pid = listed[0]["id"]
        r = c.post(f"/api/pm/triage/{pid}/accept",
                   json={"edits": {"impact": 5, "effort": 4}},
                   headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["triageState"] == "classified"

        async def _check():
            async with db_session() as db:
                w = (await db.execute(select(WorkItem).where(
                    WorkItem.number == 1))).scalar_one()
                assert w.impact == 5 and w.effort == 4      # edits applied
                assert w.reach == 3                          # untouched copy
                assert w.classified_by == "human"
        c.portal.call(_check)


def test_triage_accept_unknown_404():
    with _client_with_stubbed_seams() as c:
        r = c.post("/api/pm/triage/nope/accept", headers=IDENTITY_HEADERS)
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Regression: phase-2 routes must work when app.state.orchestrator is None
# (i.e. remote/edge mode where app.state.sessions is the only adapter).
# ---------------------------------------------------------------------------

def test_phase2_routes_work_when_orchestrator_is_none():
    """Regression: phase-2 routes must NOT raise AttributeError when
    app.state.orchestrator is None (remote/edge mode). Before the fix all three
    routes accessed request.app.state.orchestrator and 500'd on the edge.

    The lifespan must complete before we can null out orchestrator, so we use
    _client_with_stubbed_seams (which passes a real SessionOrchestrator) and then
    set app.state.orchestrator=None INSIDE the 'with' block (after startup), which
    is the exact runtime state of a remote/edge process mid-request."""
    with _client_with_stubbed_seams() as c:
        # Null out orchestrator to simulate remote/edge mid-request state.
        # The TestClient is already running; lifespan completed successfully.
        # The three phase-2 routes previously read app.state.orchestrator directly
        # and would raise AttributeError here — now they must read app.state.sessions.
        app = c.app  # type: ignore[attr-defined]
        saved_orch = app.state.orchestrator
        app.state.orchestrator = None  # type: ignore[assignment]
        try:
            # Create a project via the session-agnostic phase-1 route.
            resp = c.post("/api/pm/projects", json={
                "name": "Edge Test",
                "repos": [{"repo": "o/edge-test", "isPrimary": True}],
            }, headers=IDENTITY_HEADERS)
            assert resp.status_code == 201, resp.text
            proj_slug = resp.json()["slug"]  # let the server derive the slug

            # GET /onboarding — before fix: AttributeError('NoneType' has no attribute
            # 'pm_onboarding_counts') → 500; after fix: 200 from app.state.sessions.
            r = c.get(f"/api/pm/projects/{proj_slug}/onboarding", headers=IDENTITY_HEADERS)
            assert r.status_code == 200, (
                f"Expected 200 from /onboarding with orchestrator=None but got "
                f"{r.status_code}: {r.text}"
            )
            body = r.json()
            assert "state" in body and "action" in body and "counts" in body

            # POST /propose-map — before fix: same AttributeError → 500; after fix: 409
            # (backfill_incomplete) because no repos have backfill_done=True.
            r = c.post(f"/api/pm/projects/{proj_slug}/propose-map", headers=IDENTITY_HEADERS)
            assert r.status_code == 409, (
                f"Expected 409 backfill_incomplete with orchestrator=None but got "
                f"{r.status_code}: {r.text}"
            )
            assert r.json()["detail"]["error"] == "backfill_incomplete"

            # POST /classify — before fix: AttributeError → 500; after fix: 409
            # (map_not_merged) because onboarding_state is 'registered'.
            r = c.post(f"/api/pm/projects/{proj_slug}/classify", headers=IDENTITY_HEADERS)
            assert r.status_code == 409, (
                f"Expected 409 map_not_merged with orchestrator=None but got "
                f"{r.status_code}: {r.text}"
            )
            assert r.json()["detail"]["error"] == "map_not_merged"
        finally:
            # Restore the orchestrator so the lifespan shutdown path can clean up.
            app.state.orchestrator = saved_orch


def test_remote_pm_onboarding_counts_returns_dataclass():
    """Regression: RemoteOrchestrator.pm_onboarding_counts must return a
    ProjectOnboardingView (not a raw dict). Before the fix, the API consumed
    view.state / view.repo_count via attribute access → AttributeError on a dict."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from server.sessions.remote import RemoteOrchestrator
    from server.pm.onboarding import ProjectOnboardingView

    rpc_result = {
        "state": "syncing",
        "repo_count": 2,
        "backfill_done_count": 1,
        "feature_count": 0,
        "open_unclassified_count": 0,
        "pending_proposal_count": 0,
    }

    client = MagicMock()
    client.call = AsyncMock(return_value=rpc_result)
    remote = RemoteOrchestrator(client)

    view = asyncio.run(remote.pm_onboarding_counts("proj-123"))

    assert isinstance(view, ProjectOnboardingView), (
        f"Expected ProjectOnboardingView, got {type(view).__name__} — "
        "dict was returned (the pre-fix bug)"
    )
    assert view.state == "syncing"
    assert view.repo_count == 2
    assert view.backfill_done_count == 1
    # Verify attribute access works — this is what the API does; a dict would raise
    # AttributeError here, which is exactly the second defect being fixed.
    assert view.pending_proposal_count == 0


# ---------------------------------------------------------------------------
# Phase-3 tests: accept-bulk, overlay PATCH, drift re-propose, projectSlug
# ---------------------------------------------------------------------------


def _seed_one_pending(c, *, feature_slug=None, no_fit_suggestion=None, repo="o/r",
                      number=1, project_slug="viberpit"):
    """Seed (optionally) a Feature plus a WorkItem and a pending TriageProposal
    against the already-registered project. Returns (proposalId, workItemId).

    Seeded through the app's OWN event loop (same idiom as
    test_cicd_findings_api._seed). A bare ``asyncio.run(_run())`` runs on a throwaway
    loop while the app holds the module-level ``server.db._engine`` on the TestClient
    portal loop. Both share one AsyncAdaptedQueuePool, and that pool's
    ``asyncio.Queue`` binds to whichever loop first *awaits* a checkout —
    ``get_nowait()`` never checks loop affinity, which is why an idle pool tolerates
    the cross-loop seed. Once the orchestrator's control loops saturate the
    5-connection pool at startup, the foreign-loop checkout has to await and dies with
    "Queue ... is bound to a different event loop".
    """
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import Feature, Project, TriageProposal, WorkItem

    async def _run():
        async with db_session() as db:
            p = (await db.execute(select(Project).where(
                Project.slug == project_slug))).scalar_one()
            feat = None
            if feature_slug:
                feat = Feature(project_id=p.id, slug=feature_slug,
                               title=feature_slug, state="live")
                db.add(feat)
                await db.flush()
            w = WorkItem(repo=repo, number=number, kind="issue", title="Fix",
                         state="open", triage_state="unclassified")
            db.add(w)
            await db.flush()
            tp = TriageProposal(
                work_item_id=w.id, rationale="looks like sessions",
                reach=3, impact=2, effort=1, confidence=2, status="pending",
                proposed_feature_id=(feat.id if feat and not no_fit_suggestion
                                     else None),
                no_fit_suggestion=no_fit_suggestion)
            db.add(tp)
            await db.flush()
            return tp.id, w.id
    return c.portal.call(_run)


def test_accept_bulk_copies_overlay():
    """accept-bulk runs the SAME copy-onto-overlay path as single-accept and
    reports per-id outcomes in a 200 aggregate."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import WorkItem

    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        pid, wid = _seed_one_pending(c, feature_slug="sessions")

        r = c.post("/api/pm/triage/accept-bulk",
                   json={"ids": [pid, "ghost"]}, headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["accepted"] == 1
        by_id = {x["id"]: x for x in body["results"]}
        assert by_id[pid]["ok"] is True
        assert by_id["ghost"]["ok"] is False
        assert by_id["ghost"]["error"] == "not_found"

        # re-accepting an already-accepted proposal is not_pending, not a 500
        r2 = c.post("/api/pm/triage/accept-bulk", json={"ids": [pid]},
                    headers=IDENTITY_HEADERS)
        assert r2.status_code == 200, r2.text
        assert r2.json()["accepted"] == 0
        assert r2.json()["results"][0]["error"] == "not_pending"

        async def _check():
            async with db_session() as db:
                w = (await db.execute(select(WorkItem).where(
                    WorkItem.id == wid))).scalar_one()
                assert w.triage_state == "classified"
                assert w.classified_by == "human"
                assert w.feature_id is not None
                assert (w.reach, w.impact, w.effort, w.confidence) == (3, 2, 1, 2)
        c.portal.call(_check)

        # the accepted proposal has left the pending queue
        listed = c.get("/api/pm/triage", headers=IDENTITY_HEADERS)
        assert listed.status_code == 200
        assert [i["id"] for i in listed.json()] == []


def test_overlay_patch_manual_classify():
    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")

        r = c.patch(f"/api/pm/work-items/{wid}/overlay",
                    json={"reach": 5, "triageState": "classified"},
                    headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["reach"] == 5
        assert body["triageState"] == "classified"
        # classified_by is the actor's identity, not a blanket "human" (spec §8).
        assert body["classifiedBy"] == "testuser@example.com"
        assert body["classifiedAt"] is not None

        bad = c.patch(f"/api/pm/work-items/{wid}/overlay",
                      json={"triageState": "bogus"}, headers=IDENTITY_HEADERS)
        assert bad.status_code == 422
        assert bad.json()["detail"]["error"] == "bad_triage_state"

        missing = c.patch("/api/pm/work-items/nope/overlay",
                          json={"reach": 1}, headers=IDENTITY_HEADERS)
        assert missing.status_code == 404
        assert missing.json()["detail"]["error"] == "not_found"

        bad_feat = c.patch(f"/api/pm/work-items/{wid}/overlay",
                           json={"featureId": "ghost"}, headers=IDENTITY_HEADERS)
        assert bad_feat.status_code == 404
        assert bad_feat.json()["detail"]["error"] == "feature_not_found"


LAN_OPEN = {Settings.model_fields["allow_unauthenticated"].alias: "1"}


def test_overlay_hold_by_person_any_kind_with_audit():
    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")
        r = c.patch(f"/api/pm/work-items/{wid}/overlay",
                    json={"holdKind": "owner_action", "holdReason": "needs the owner's keyring"},
                    headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["holdKind"] == "owner_action"
        assert body["holdReason"] == "needs the owner's keyring"
        assert body["holdBy"] == "testuser@example.com"
        assert body["holdSetAt"] is not None
        assert body["classifiedBy"] == "testuser@example.com"
        # reason is mandatory
        bad = c.patch(f"/api/pm/work-items/{wid}/overlay",
                      json={"holdKind": "evidence"}, headers=IDENTITY_HEADERS)
        assert bad.status_code == 422
        assert bad.json()["detail"]["error"] == "hold_reason_required"
        # clear
        r = c.patch(f"/api/pm/work-items/{wid}/overlay", json={"clearHold": True},
                    headers=IDENTITY_HEADERS)
        assert r.status_code == 200 and r.json()["holdKind"] is None
        from server.db import db_session
        from sqlalchemy import select
        from server.db.models import AuditEvent

        async def _types():
            async with db_session() as db:
                return [(e.type, e.detail, e.via) for e in (await db.execute(
                    select(AuditEvent).where(AuditEvent.type.like("pm.hold.%"))
                    .order_by(AuditEvent.at))).scalars().all()]
        events = c.portal.call(_types)
        assert [t for t, _, _ in events] == ["pm.hold.set", "pm.hold.release"]
        assert events[0][1]["kind"] == "owner_action" and events[0][1]["actorClass"] == "person"
        # ONE release audit shape across both API paths (F5): every release names
        # the signal that ended the hold, and an API release is ``via="api"``.
        from server.pm.holds import RELEASE_SIGNALS
        assert events[1][1]["signal"] == "overlay" and "overlay" in RELEASE_SIGNALS
        assert events[1][2] == "api"


def test_overlay_hold_by_loopback_caller_is_limited_and_never_human():
    with patch.dict(os.environ, LAN_OPEN), _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")
        # no identity header → lan@local (LAN-open mode); an agent-class actor
        r = c.patch(f"/api/pm/work-items/{wid}/overlay",
                    json={"holdKind": "evidence", "holdReason": "correlate with #2"})
        assert r.status_code == 200, r.text
        assert r.json()["holdBy"] == "lan@local"
        assert r.json()["classifiedBy"] == "lan@local"
        refused = c.patch(f"/api/pm/work-items/{wid}/overlay",
                          json={"holdKind": "owner_action", "holdReason": "park it"})
        assert refused.status_code == 422
        assert refused.json()["detail"]["error"] == "hold_not_permitted"
        # a person parks it; the loopback caller cannot release that
        r = c.patch(f"/api/pm/work-items/{wid}/overlay",
                    json={"holdKind": "owner_action", "holdReason": "owner only"},
                    headers=IDENTITY_HEADERS)
        assert r.status_code == 200
        refused = c.patch(f"/api/pm/work-items/{wid}/overlay", json={"clearHold": True})
        assert refused.status_code == 422
        assert refused.json()["detail"]["error"] == "hold_not_permitted"
        # …nor OVERWRITE it with a kind it may set, which would release it just as
        # surely as clearHold (final-review F1).
        refused = c.patch(f"/api/pm/work-items/{wid}/overlay",
                          json={"holdKind": "evidence", "holdReason": "correlate with #2"})
        assert refused.status_code == 422
        assert refused.json()["detail"]["error"] == "hold_not_permitted"
        still = c.patch(f"/api/pm/work-items/{wid}/overlay", json={},
                        headers=IDENTITY_HEADERS).json()
        assert still["holdKind"] == "owner_action"
        assert still["holdBy"] == "testuser@example.com"
        # a legacy value is no longer accepted anywhere
        legacy = c.patch(f"/api/pm/work-items/{wid}/overlay",
                         json={"triageState": "needs_human"}, headers=IDENTITY_HEADERS)
        assert legacy.status_code == 422


def test_reject_by_loopback_caller_records_agent_actor():
    """Rejecting is one of the two flows spec §8 authorises to write ``owner_action``
    regardless of actor class, so a LAN-open (agent-class) rejection must SUCCEED —
    the Reject button has to work there — while recording the truth about who did it:
    ``hold_by == "lan@local"`` and ``actorClass == "agent"``, never a person's login
    (final-review F4)."""
    from server.db import db_session
    from sqlalchemy import select
    from server.db.models import AuditEvent, TriageProposal, WorkItem

    with patch.dict(os.environ, LAN_OPEN), _client() as c:
        _register(c, "Viberpit", repo="o/r")
        pid, wid = _seed_one_pending(c, feature_slug="sessions")
        r = c.post(f"/api/pm/triage/{pid}/reject")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "rejected"

        async def _check():
            async with db_session() as db:
                prop = (await db.execute(select(TriageProposal).where(
                    TriageProposal.id == pid))).scalar_one()
                wi = (await db.execute(select(WorkItem).where(
                    WorkItem.id == wid))).scalar_one()
                detail = (await db.execute(select(AuditEvent.detail).where(
                    AuditEvent.type == "pm.hold.set"))).scalars().one()
                return prop.status, wi.hold_kind, wi.hold_by, wi.hold_set_at, detail
        status, hold_kind, hold_by, hold_set_at, detail = c.portal.call(_check)
        assert status == "rejected"
        assert hold_kind == "owner_action"
        assert hold_by == "lan@local"
        assert hold_set_at is not None
        assert detail["actorClass"] == "agent"


def test_overlay_hold_reason_without_hold_kind_is_bad_hold():
    """A bare holdReason with no holdKind names nothing to hold and must be
    rejected outright (#task-3 review finding 2), not silently ignored."""
    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")
        r = c.patch(f"/api/pm/work-items/{wid}/overlay",
                    json={"holdReason": "x"}, headers=IDENTITY_HEADERS)
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "bad_hold"


def test_overlay_clear_hold_on_unheld_item_is_a_true_no_op():
    """clearHold on an item with no hold must not stamp classified_by or fire the
    overlay audit — clear_hold no-ops internally, and the route must not treat
    that as an applied change (#task-3 review finding 2)."""
    from server.db import db_session
    from sqlalchemy import select
    from server.db.models import AuditEvent

    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")
        pre = c.patch(f"/api/pm/work-items/{wid}/overlay", json={},
                      headers=IDENTITY_HEADERS).json()
        assert pre["holdKind"] is None

        async def _count():
            async with db_session() as db:
                return len((await db.execute(select(AuditEvent).where(
                    AuditEvent.type == "pm.workitem.overlay"))).scalars().all())
        before = c.portal.call(_count)

        r = c.patch(f"/api/pm/work-items/{wid}/overlay", json={"clearHold": True},
                    headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["classifiedBy"] == pre["classifiedBy"]
        assert r.json()["classifiedAt"] == pre["classifiedAt"]

        after = c.portal.call(_count)
        assert after == before


def test_work_item_outcomes_endpoint():
    """`GET /api/pm/work-items/{id}/outcomes` — every dispatch this issue has had,
    newest first, camelCase keys (spec §6/§7); a 404 shape for an unknown item."""
    from datetime import datetime, timezone

    from server.db import db_session
    from server.db.models import DispatchOutcome

    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")

        async def _seed_outcomes():
            async with db_session() as db:
                db.add(DispatchOutcome(
                    target_ref="o/r#1", outcome="no_pr", cause="tests_failed",
                    attempt_no=1, body_fingerprint="fp1",
                    created_at=datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)))
                db.add(DispatchOutcome(
                    target_ref="o/r#1", outcome="merged", pr_number=42,
                    attempt_no=2, body_fingerprint="fp1", first_pass_merge=True,
                    created_at=datetime(2026, 9, 10, 12, 5, 0, tzinfo=timezone.utc)))
        c.portal.call(_seed_outcomes)

        r = c.get(f"/api/pm/work-items/{wid}/outcomes", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        assert [i["outcome"] for i in items] == ["merged", "no_pr"]   # newest first
        assert items[0]["prNumber"] == 42
        assert items[0]["firstPassMerge"] is True
        assert items[0]["attemptNo"] == 2
        assert items[1]["cause"] == "tests_failed"
        assert items[1]["attemptNo"] == 1

        missing = c.get("/api/pm/work-items/nope/outcomes", headers=IDENTITY_HEADERS)
        assert missing.status_code == 404
        assert missing.json()["detail"]["error"] == "not_found"


def test_drift_accept_reproposes_orphaned_siblings():
    """Two no_fit items share a suggestion; the drift map merged a matching
    Feature. Accepting one links it AND re-proposes the sibling against the new
    feature as a fresh pending proposal (design §6.3)."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import Feature, Project, TriageProposal, WorkItem

    with _client() as c:
        _register(c, "Viberpit", repo="o/r")

        async def _seed():
            async with db_session() as db:
                p = (await db.execute(select(Project).where(
                    Project.slug == "viberpit"))).scalar_one()
                feat = Feature(project_id=p.id, slug="offline-mode",
                               title="Offline mode", state="live")
                db.add(feat)
                await db.flush()
                ids = []
                for n in (1, 2):
                    w = WorkItem(repo="o/r", number=n, kind="issue",
                                 title=f"#{n}", state="open",
                                 triage_state="unclassified")
                    db.add(w)
                    await db.flush()
                    tp = TriageProposal(work_item_id=w.id, status="pending",
                                        no_fit_suggestion="offline-mode",
                                        rationale="offline")
                    db.add(tp)
                    await db.flush()
                    ids.append((tp.id, w.id))
                return ids
        (pid1, wid1), (pid2, _wid2) = c.portal.call(_seed)

        r = c.post("/api/pm/triage/accept-bulk", json={"ids": [pid1]},
                   headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["accepted"] == 1

        async def _check():
            async with db_session() as db:
                sib = (await db.execute(select(TriageProposal).where(
                    TriageProposal.id == pid2))).scalar_one()
                accepted_item = (await db.execute(select(WorkItem).where(
                    WorkItem.id == wid1))).scalar_one()
                return (sib.status, sib.proposed_feature_id,
                        sib.no_fit_suggestion, accepted_item.feature_id)
        status_, fid, no_fit, accepted_fid = c.portal.call(_check)
        # the sibling stays pending but now points at the newly-merged feature
        assert status_ == "pending"
        assert fid is not None
        assert no_fit is None
        # the accepted item itself was linked to the drift-resolved feature
        assert accepted_fid == fid


def test_drift_accept_handles_two_siblings_in_one_batch():
    """Both halves of one drift cluster in the SAME ids list. Accepting the first
    re-points the second (clearing its no_fit_suggestion), so the second id must
    still accept cleanly against its freshly-set proposed_feature_id — the batch
    loop has to be re-entrant across its own side effects."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import Feature, Project, TriageProposal, WorkItem

    with _client() as c:
        _register(c, "Viberpit", repo="o/r")

        async def _seed():
            async with db_session() as db:
                p = (await db.execute(select(Project).where(
                    Project.slug == "viberpit"))).scalar_one()
                db.add(Feature(project_id=p.id, slug="offline-mode",
                               title="Offline mode", state="live"))
                await db.flush()
                ids = []
                for n in (1, 2):
                    w = WorkItem(repo="o/r", number=n, kind="issue",
                                 title=f"#{n}", state="open",
                                 triage_state="unclassified")
                    db.add(w)
                    await db.flush()
                    tp = TriageProposal(work_item_id=w.id, status="pending",
                                        no_fit_suggestion="offline-mode",
                                        rationale="offline")
                    db.add(tp)
                    await db.flush()
                    ids.append((tp.id, w.id))
                return ids
        (pid1, wid1), (pid2, wid2) = c.portal.call(_seed)

        r = c.post("/api/pm/triage/accept-bulk", json={"ids": [pid1, pid2]},
                   headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["accepted"] == 2, r.text
        assert all(x["ok"] for x in r.json()["results"])

        async def _check():
            async with db_session() as db:
                items = (await db.execute(select(WorkItem).where(
                    WorkItem.id.in_([wid1, wid2])))).scalars().all()
                return {w.id: w.feature_id for w in items}
        by_id = c.portal.call(_check)
        assert by_id[wid1] is not None
        assert by_id[wid1] == by_id[wid2]


def test_drift_accept_via_single_accept_route():
    """The drift branch lives in the shared ``apply_accept`` helper, so plain
    single-accept inherits it: a no_fit whose suggestion is now a real feature
    links to that feature and re-proposes its siblings."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import Feature, Project, TriageProposal, WorkItem

    with _client() as c:
        _register(c, "Viberpit", repo="o/r")

        async def _seed():
            async with db_session() as db:
                p = (await db.execute(select(Project).where(
                    Project.slug == "viberpit"))).scalar_one()
                db.add(Feature(project_id=p.id, slug="offline-mode",
                               title="Offline mode", state="live"))
                await db.flush()
                ids = []
                for n in (1, 2):
                    w = WorkItem(repo="o/r", number=n, kind="issue",
                                 title=f"#{n}", state="open",
                                 triage_state="unclassified")
                    db.add(w)
                    await db.flush()
                    tp = TriageProposal(work_item_id=w.id, status="pending",
                                        no_fit_suggestion="offline-mode",
                                        rationale="offline")
                    db.add(tp)
                    await db.flush()
                    ids.append((tp.id, w.id))
                return ids
        (pid1, wid1), (pid2, _wid2) = c.portal.call(_seed)

        r = c.post(f"/api/pm/triage/{pid1}/accept", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text

        async def _check():
            async with db_session() as db:
                w = (await db.execute(select(WorkItem).where(
                    WorkItem.id == wid1))).scalar_one()
                sib = (await db.execute(select(TriageProposal).where(
                    TriageProposal.id == pid2))).scalar_one()
                return w.feature_id, sib.proposed_feature_id, sib.no_fit_suggestion
        fid, sib_fid, sib_no_fit = c.portal.call(_check)
        assert fid is not None
        assert sib_fid == fid
        assert sib_no_fit is None


def test_drift_accept_never_binds_another_projects_feature():
    """Regression: Feature.slug is unique only per project (uq_feature_slug), so
    the drift lookup MUST be scoped to the work item's OWN project. Alpha owns a
    Feature 'offline-mode'; a beta work item's drift accept must never bind to
    it, and beta's own pending sibling must be left alone."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import Feature, Project, TriageProposal, WorkItem

    with _client() as c:
        _register(c, "Alpha", repo="o/alpha")
        _register(c, "Beta", repo="o/beta")

        async def _seed():
            async with db_session() as db:
                alpha = (await db.execute(select(Project).where(
                    Project.slug == "alpha"))).scalar_one()
                feat = Feature(project_id=alpha.id, slug="offline-mode",
                               title="Offline mode", state="live")
                db.add(feat)
                await db.flush()
                out = {"alphaFeatureId": feat.id}
                for repo, n in (("o/beta", 1), ("o/beta", 2)):
                    w = WorkItem(repo=repo, number=n, kind="issue",
                                 title=f"#{n}", state="open",
                                 triage_state="unclassified")
                    db.add(w)
                    await db.flush()
                    tp = TriageProposal(work_item_id=w.id, status="pending",
                                        no_fit_suggestion="offline-mode",
                                        rationale="offline")
                    db.add(tp)
                    await db.flush()
                    out[f"p{n}"] = tp.id
                    out[f"w{n}"] = w.id
                return out
        seeded = c.portal.call(_seed)

        r = c.post("/api/pm/triage/accept-bulk",
                   json={"ids": [seeded["p1"]]}, headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text

        async def _check():
            async with db_session() as db:
                w = (await db.execute(select(WorkItem).where(
                    WorkItem.id == seeded["w1"]))).scalar_one()
                sib = (await db.execute(select(TriageProposal).where(
                    TriageProposal.id == seeded["p2"]))).scalar_one()
                return w.feature_id, sib.proposed_feature_id, sib.no_fit_suggestion
        fid, sib_fid, sib_no_fit = c.portal.call(_check)
        # NEVER alpha's feature — beta owns no matching feature, so no link at all.
        assert fid != seeded["alphaFeatureId"]
        assert fid is None
        # …and the cross-project sibling sweep must not have fired either.
        assert sib_fid != seeded["alphaFeatureId"]
        assert sib_no_fit == "offline-mode"


def test_accept_bulk_one_failure_does_not_roll_back_the_batch():
    """Regression: a non-HTTPException from one id used to escape the route, so
    get_db rolled the WHOLE session back — the already-flushed accepts of the
    earlier ids were reverted and the caller got a 500. Each id now runs in its
    own savepoint and the batch stays a 200 aggregate."""
    from sqlalchemy import select

    from server.api import pm as pm_api
    from server.db import db_session
    from server.db.models import TriageProposal, WorkItem

    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        pid1, wid1 = _seed_one_pending(c, feature_slug="sessions", number=1)
        pid2, wid2 = _seed_one_pending(c, feature_slug="queue", number=2)

        # Patched through the ROUTER's reference (the name the route body resolves),
        # not the owning module's, so the seam is the call site under test.
        real_apply = pm_api.apply_accept

        async def _flaky(db, proposal, *, decided_by, audit_identity, edits=None, ownership=None):
            # Mutate first, THEN blow up: proves the savepoint discards this id's
            # partial work while leaving the sibling ids' work committed.
            await real_apply(db, proposal, decided_by=decided_by,
                             audit_identity=audit_identity, edits=edits, ownership=ownership)
            if proposal.id == pid2:
                raise RuntimeError("boom")

        with patch.object(pm_api, "apply_accept", _flaky):
            r = c.post("/api/pm/triage/accept-bulk",
                       json={"ids": [pid1, pid2]}, headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["accepted"] == 1
        by_id = {x["id"]: x for x in body["results"]}
        assert by_id[pid1]["ok"] is True
        assert by_id[pid2] == {"id": pid2, "ok": False, "error": "failed"}

        async def _check():
            async with db_session() as db:
                items = {w.id: w for w in (await db.execute(select(WorkItem).where(
                    WorkItem.id.in_([wid1, wid2])))).scalars().all()}
                props = {p.id: p for p in (await db.execute(
                    select(TriageProposal).where(
                        TriageProposal.id.in_([pid1, pid2])))).scalars().all()}
                return ((items[wid1].triage_state, props[pid1].status),
                        (items[wid2].triage_state, props[pid2].status))
        first, second = c.portal.call(_check)
        assert first == ("classified", "accepted")      # survived the sibling's failure
        assert second == ("unclassified", "pending")    # rolled back to itself only


def _delete_work_item(c, work_item_id):
    """Drop the WorkItem row out from under a still-pending proposal.

    The real shape this stands in for: the GitHub reconciler deletes a work item
    (issue transferred, repo unmapped) between the proposal being written and a
    human deciding it. ``apply_accept`` then raises ``WorkItemMissing``, and both
    accept routes owe a specific answer rather than a 500."""
    from sqlalchemy import delete

    from server.db import db_session
    from server.db.models import WorkItem

    async def _run():
        async with db_session() as db:
            await db.execute(delete(WorkItem).where(WorkItem.id == work_item_id))
    c.portal.call(_run)


def test_accept_404s_when_the_work_item_row_is_gone():
    """``WorkItemMissing`` out of the shared ``apply_accept`` maps to a 404
    ``work_item_missing`` — not the 500 an unhandled LookupError would be, and
    not the generic ``not_found`` the proposal lookup answers (the proposal is
    right there; it is the item it names that vanished)."""
    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        pid, wid = _seed_one_pending(c, feature_slug="sessions")
        _delete_work_item(c, wid)

        r = c.post(f"/api/pm/triage/{pid}/accept", headers=IDENTITY_HEADERS)
        assert r.status_code == 404, r.text
        assert r.json()["detail"] == {"error": "work_item_missing"}


def test_accept_bulk_reports_work_item_missing_per_id():
    """The per-id twin: one id's missing work item is that id's failure, with
    the SAME ``work_item_missing`` code single-accept 404s with — not the
    generic ``failed`` the catch-all branch would downgrade it to, and not a
    batch-wide error that would lose the ids that did land."""
    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        gone_pid, gone_wid = _seed_one_pending(c, feature_slug="sessions", number=1)
        good_pid, good_wid = _seed_one_pending(c, feature_slug="queue", number=2)
        _delete_work_item(c, gone_wid)

        r = c.post("/api/pm/triage/accept-bulk",
                   json={"ids": [gone_pid, good_pid]}, headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["accepted"] == 1
        by_id = {x["id"]: x for x in body["results"]}
        assert by_id[gone_pid] == {"id": gone_pid, "ok": False,
                                   "error": "work_item_missing"}
        assert by_id[good_pid] == {"id": good_pid, "ok": True}

        # …and the savepoint left the good id's work committed.
        from sqlalchemy import select

        from server.db import db_session
        from server.db.models import TriageProposal, WorkItem

        async def _check():
            async with db_session() as db:
                w = (await db.execute(select(WorkItem).where(
                    WorkItem.id == good_wid))).scalar_one()
                props = {p.id: p.status for p in (await db.execute(
                    select(TriageProposal).where(TriageProposal.id.in_(
                        [gone_pid, good_pid])))).scalars().all()}
                return w.triage_state, props
        state, props = c.portal.call(_check)
        assert state == "classified"
        assert props[good_pid] == "accepted"
        assert props[gone_pid] == "pending"     # untouched, still decidable


def test_accept_rejects_out_of_range_rice_edits():
    """Accept-with-edits writes the same four numbers the overlay PATCH does, so
    it needs the same range guard.

    The inbox used to PATCH the overlay first and get this validation for free;
    it no longer does (the pre-accept PATCH corrupted the undo snapshot, which
    is taken from the overlay AS IT WAS before the decision). Without this the
    only thing standing between a typo and an inverted roadmap ordering was the
    browser.
    """
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import TriageProposal, WorkItem

    with _client() as c:
        _register(c, "viberpit", repo="o/r")
        pid, wid = _seed_one_pending(c, feature_slug="sessions")

        for edits in ({"effort": 0}, {"effort": -1}, {"reach": -1},
                      {"impact": 10001}, {"confidence": -5}, {"reach": "lots"}):
            bad = c.post(f"/api/pm/triage/{pid}/accept", json={"edits": edits},
                         headers=IDENTITY_HEADERS)
            assert bad.status_code == 422, (edits, bad.text)
            assert bad.json()["detail"]["error"] == "bad_rice_value"

        # Refused BEFORE anything was written: the proposal is still decidable.
        async def _check():
            async with db_session() as db:
                prop = (await db.execute(select(TriageProposal).where(
                    TriageProposal.id == pid))).scalar_one()
                wi = (await db.execute(select(WorkItem).where(
                    WorkItem.id == wid))).scalar_one()
                return prop.status, wi.triage_state
        assert c.portal.call(_check)[0] == "pending"

        ok = c.post(f"/api/pm/triage/{pid}/accept",
                    json={"edits": {"effort": 1, "reach": 0, "impact": 10000,
                                    "confidence": 0}},
                    headers=IDENTITY_HEADERS)
        assert ok.status_code == 200, ok.text


def test_accept_rejects_boolean_rice_edits():
    """`True` IS an `int` in Python, so a bare isinstance check waves it through
    and `effort: true` lands in the overlay as a 1 nobody typed — a real RICE
    score, silently invented from a JSON type error."""
    with _client() as c:
        _register(c, "viberpit", repo="o/r")
        pid, _wid = _seed_one_pending(c, feature_slug="sessions")
        for edits in ({"effort": True}, {"reach": False}, {"impact": True},
                      {"confidence": False}):
            bad = c.post(f"/api/pm/triage/{pid}/accept", json={"edits": edits},
                         headers=IDENTITY_HEADERS)
            assert bad.status_code == 422, (edits, bad.text)
            assert bad.json()["detail"]["error"] == "bad_rice_value"
            assert "whole number" in bad.json()["detail"]["message"]


def test_accept_with_edits_rejects_an_unknown_feature_id():
    """`featureId` in an accept body writes `WorkItem.feature_id` directly, the
    same column the overlay PATCH writes — and the PATCH 404s an id that names no
    row. Accept-with-edits replaced that PATCH, so it has to answer the same way;
    without this the item points at a feature that does not exist and every
    consumer keyed on feature→project reads a dangling id."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import TriageProposal, WorkItem

    with _client() as c:
        _register(c, "viberpit", repo="o/r")
        pid, wid = _seed_one_pending(c, feature_slug="sessions")

        bad = c.post(f"/api/pm/triage/{pid}/accept",
                     json={"edits": {"featureId": "f-does-not-exist"}},
                     headers=IDENTITY_HEADERS)
        assert bad.status_code == 404, bad.text
        assert bad.json()["detail"]["error"] == "feature_not_found"

        # Refused BEFORE anything was written: the proposal is still decidable.
        async def _check():
            async with db_session() as db:
                prop = (await db.execute(select(TriageProposal).where(
                    TriageProposal.id == pid))).scalar_one()
                wi = (await db.execute(select(WorkItem).where(
                    WorkItem.id == wid))).scalar_one()
                return prop.status, wi.triage_state, prop.proposed_feature_id
        status_, state, real_feature_id = c.portal.call(_check)
        assert (status_, state) == ("pending", "unclassified")

        # An id that DOES name a feature still accepts.
        ok = c.post(f"/api/pm/triage/{pid}/accept",
                    json={"edits": {"featureId": real_feature_id}},
                    headers=IDENTITY_HEADERS)
        assert ok.status_code == 200, ok.text

        # An explicit null is "no home", not a lookup — it must not 404.
        pid2, _wid2 = _seed_one_pending(c, number=91)
        ok2 = c.post(f"/api/pm/triage/{pid2}/accept",
                     json={"edits": {"featureId": None}}, headers=IDENTITY_HEADERS)
        assert ok2.status_code == 200, ok2.text


def test_overlay_patch_rejects_out_of_range_rice_values():
    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")

        for payload in ({"effort": 0}, {"effort": -1}, {"reach": -1},
                        {"impact": 10001}, {"confidence": -5}):
            bad = c.patch(f"/api/pm/work-items/{wid}/overlay", json=payload,
                          headers=IDENTITY_HEADERS)
            assert bad.status_code == 422, (payload, bad.text)
            assert bad.json()["detail"]["error"] == "bad_rice_value"

        ok = c.patch(f"/api/pm/work-items/{wid}/overlay",
                     json={"effort": 1, "reach": 0, "impact": 10000,
                           "confidence": 0}, headers=IDENTITY_HEADERS)
        assert ok.status_code == 200, ok.text


def test_overlay_patch_with_empty_body_is_a_no_op():
    """An empty PATCH must not flip an AI-classified item to human-classified:
    there was no human decision to record."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import AuditEvent, WorkItem

    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")

        async def _mark_ai():
            async with db_session() as db:
                w = (await db.execute(select(WorkItem).where(
                    WorkItem.id == wid))).scalar_one()
                w.classified_by = "ai"
                w.classified_at = None
        c.portal.call(_mark_ai)

        r = c.patch(f"/api/pm/work-items/{wid}/overlay", json={},
                    headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["classifiedBy"] == "ai"
        assert r.json()["classifiedAt"] is None

        async def _check():
            async with db_session() as db:
                w = (await db.execute(select(WorkItem).where(
                    WorkItem.id == wid))).scalar_one()
                audits = (await db.execute(select(AuditEvent).where(
                    AuditEvent.type == "pm.workitem.overlay"))).scalars().all()
                return w.classified_by, w.classified_at, len(audits)
        by, at, n_audit = c.portal.call(_check)
        assert (by, at, n_audit) == ("ai", None, 0)


def test_triage_rows_carry_project_slug():
    """The inbox header groups by project, so every row resolves its project
    server-side via ProjectRepo.repo → Project.slug (the client must not guess)."""
    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _seed_one_pending(c, feature_slug="sessions", repo="o/r", number=1)
        # an item whose repo belongs to no project → projectSlug is null
        _seed_one_pending(c, feature_slug=None, repo="other/unowned", number=7)

        rows = c.get("/api/pm/triage", headers=IDENTITY_HEADERS)
        assert rows.status_code == 200, rows.text
        by_repo = {row["repo"]: row for row in rows.json()}
        assert by_repo["o/r"]["projectSlug"] == "viberpit"
        assert by_repo["other/unowned"]["projectSlug"] is None

        # the projectSlug filter still narrows the list
        filtered = c.get("/api/pm/triage?projectSlug=viberpit",
                         headers=IDENTITY_HEADERS)
        assert filtered.status_code == 200
        assert [row["repo"] for row in filtered.json()] == ["o/r"]


def test_onboarding_view_carries_the_map_pr_search_link():
    """The wizard's await-map-PR step needs somewhere to send the user. The PR
    number is not stored, but the branch is deterministic, so the view carries
    a GitHub PR search on that branch for the primary repo."""
    with _client_with_stubbed_seams() as c:
        _register(c, "Viberpit", repo="o/r")
        r = c.get("/api/pm/projects/viberpit/onboarding", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        map_pr = r.json()["mapPr"]
        assert map_pr["branch"] == "pm/feature-map-viberpit"
        assert map_pr["url"] == (
            "https://github.com/o/r/pulls?q=is%3Apr+head%3Apm%2Ffeature-map-viberpit")


def test_advance_live_transitions_state():
    """advance-live: 200 from map_merged or classifying → live; 409 otherwise."""
    from server.db import db_session
    from sqlalchemy import select
    from server.db.models import Project

    with _client_with_stubbed_seams() as c:
        _register(c, "Viberpit", repo="o/r")

        r = c.post("/api/pm/projects/viberpit/advance-live", headers=IDENTITY_HEADERS)
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "not_classifying"

        async def _set(state):
            async with db_session() as db:
                p = (await db.execute(select(Project).where(
                    Project.slug == "viberpit"))).scalar_one()
                p.onboarding_state = state
        assert c.portal is not None
        c.portal.call(_set, "classifying")

        r = c.post("/api/pm/projects/viberpit/advance-live", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["onboardingState"] == "live"

        async def _check():
            async with db_session() as db:
                p = (await db.execute(select(Project).where(
                    Project.slug == "viberpit"))).scalar_one()
                assert p.onboarding_state == "live"
        c.portal.call(_check)

        # live is absorbing: a second tap is a 409, not a silent no-op
        r = c.post("/api/pm/projects/viberpit/advance-live", headers=IDENTITY_HEADERS)
        assert r.status_code == 409


def test_triage_undo_restores_overlay_and_pending():
    """Controller ruling: the seed's starting triageState is 'unclassified', not
    the literal 'proposed' — read the pre-accept overlay and assert undo restores
    exactly THOSE values, rather than hardcoding a state the seed never sets."""
    with _client() as c:
        _register(c, "viberpit", repo="o/r")
        pid, wid = _seed_one_pending(c, feature_slug="sessions")
        pre = c.patch(f"/api/pm/work-items/{wid}/overlay", json={},
                      headers=IDENTITY_HEADERS).json()
        r = c.post(f"/api/pm/triage/{pid}/accept", json={"edits": {"impact": 9}},
                   headers=IDENTITY_HEADERS)
        assert r.status_code == 200
        r = c.post(f"/api/pm/triage/{pid}/undo", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json() == {"id": pid, "status": "pending", "workItemId": wid,
                            "triageState": pre["triageState"]}
        # the proposal is pending again and listed
        assert any(p["id"] == pid for p in c.get("/api/pm/triage", headers=IDENTITY_HEADERS).json())
        # the overlay is back to its pre-accept values
        r = c.patch(f"/api/pm/work-items/{wid}/overlay", json={}, headers=IDENTITY_HEADERS)
        body = r.json()
        assert body["triageState"] == pre["triageState"]
        assert body["impact"] == pre["impact"] and body["featureId"] == pre["featureId"]
        assert body["classifiedBy"] == pre["classifiedBy"]
        assert body["classifiedAt"] == pre["classifiedAt"]
        # a second undo is refused: it is pending, not resolved
        assert c.post(f"/api/pm/triage/{pid}/undo", headers=IDENTITY_HEADERS).status_code == 409
        assert c.post(f"/api/pm/triage/{pid}/undo",
                      headers=IDENTITY_HEADERS).json()["detail"]["error"] == "not_resolved"
        # and it can be accepted again
        assert c.post(f"/api/pm/triage/{pid}/accept", headers=IDENTITY_HEADERS).status_code == 200


def test_triage_undo_after_reject():
    """Same controller ruling as above: assert undo restores the pre-reject
    overlay values (whatever the seed set them to), not a hardcoded literal."""
    with _client() as c:
        _register(c, "viberpit", repo="o/r")
        pid, wid = _seed_one_pending(c, feature_slug="sessions")
        pre = c.patch(f"/api/pm/work-items/{wid}/overlay", json={},
                      headers=IDENTITY_HEADERS).json()
        assert c.post(f"/api/pm/triage/{pid}/reject", headers=IDENTITY_HEADERS).status_code == 200
        rejected = c.patch(f"/api/pm/work-items/{wid}/overlay", json={},
                           headers=IDENTITY_HEADERS).json()
        # reject no longer moves triage_state to the removed needs_human value —
        # it parks the item with an owner-action hold instead (spec 2026-09-18 §3).
        assert rejected["triageState"] == pre["triageState"]
        assert rejected["holdKind"] == "owner_action"
        r = c.post(f"/api/pm/triage/{pid}/undo", headers=IDENTITY_HEADERS)
        assert r.status_code == 200
        assert r.json()["triageState"] == pre["triageState"]
        after = c.patch(f"/api/pm/work-items/{wid}/overlay", json={},
                        headers=IDENTITY_HEADERS).json()
        assert after["triageState"] == pre["triageState"]
        assert after["impact"] == pre["impact"] and after["featureId"] == pre["featureId"]
        assert after["classifiedBy"] == pre["classifiedBy"]
        assert after["classifiedAt"] == pre["classifiedAt"]
        assert after["holdKind"] == pre["holdKind"]


def test_triage_undo_unknown_and_expired(monkeypatch):
    import server.api.pm as pm_api
    with _client() as c:
        _register(c, "viberpit", repo="o/r")
        assert c.post("/api/pm/triage/nope/undo", headers=IDENTITY_HEADERS).status_code == 404
        pid, _ = _seed_one_pending(c, feature_slug="sessions")
        assert c.post(f"/api/pm/triage/{pid}/accept", headers=IDENTITY_HEADERS).status_code == 200
        monkeypatch.setattr(pm_api, "TRIAGE_UNDO_WINDOW_S", 0)
        r = c.post(f"/api/pm/triage/{pid}/undo", headers=IDENTITY_HEADERS)
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "undo_expired"


def test_triage_undo_writes_audit_row():
    """No GET /api/audit route exists — read the row the DB-side way, following
    the idiom in test_overlay_patch_with_empty_body_is_a_no_op."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import AuditEvent

    with _client() as c:
        _register(c, "viberpit", repo="o/r")
        pid, wid = _seed_one_pending(c, feature_slug="sessions")
        assert c.post(f"/api/pm/triage/{pid}/accept", headers=IDENTITY_HEADERS).status_code == 200
        assert c.post(f"/api/pm/triage/{pid}/undo", headers=IDENTITY_HEADERS).status_code == 200

        async def _check():
            async with db_session() as db:
                return (await db.execute(select(AuditEvent).where(
                    AuditEvent.type == "pm.triage.undo"))).scalars().all()
        found = c.portal.call(_check)
        assert len(found) == 1
        assert found[0].detail["proposalId"] == pid and found[0].detail["workItemId"] == wid
        assert found[0].detail["previousStatus"] == "accepted"


def test_triage_undo_restores_classified_at_across_naive_sqlite_roundtrip():
    """Fix-round-1 regression: on SQLite a DateTime(timezone=True) column hands
    back a NAIVE datetime after a round trip through the DB — the exact slip the
    DB clock rule exists for. Pre-set classified_at directly (simulating a prior
    classification), accept (forcing apply_accept to re-query the WorkItem fresh
    off SQLite, which comes back naive), undo, and assert the restored
    classifiedAt is the SAME INSTANT as what was set — compared as parsed aware
    datetimes, not strings, so a silent naive/aware drift would fail the
    comparison rather than being masked by string formatting."""
    from datetime import datetime, timezone

    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import WorkItem

    prior_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

    with _client() as c:
        _register(c, "viberpit", repo="o/r")
        pid, wid = _seed_one_pending(c, feature_slug="sessions")

        async def _preset():
            async with db_session() as db:
                w = (await db.execute(select(WorkItem).where(
                    WorkItem.id == wid))).scalar_one()
                w.classified_by = "ai"
                w.classified_at = prior_at
        c.portal.call(_preset)

        assert c.post(f"/api/pm/triage/{pid}/accept",
                      headers=IDENTITY_HEADERS).status_code == 200
        assert c.post(f"/api/pm/triage/{pid}/undo",
                      headers=IDENTITY_HEADERS).status_code == 200

        body = c.patch(f"/api/pm/work-items/{wid}/overlay", json={},
                       headers=IDENTITY_HEADERS).json()
        assert body["classifiedBy"] == "ai"
        restored = datetime.fromisoformat(body["classifiedAt"])
        # Deliberately NOT coerced here: a server-side naive/aware slip must fail
        # this assertion, not be silently healed by the test.
        assert restored.tzinfo is not None, (
            f"classifiedAt round-tripped without a UTC offset ({body['classifiedAt']!r}) "
            "— naive/aware slip")
        assert restored == prior_at


def test_triage_undo_no_snapshot_409():
    """A row resolved before undo_snapshot existed (or otherwise cleared) answers
    409 no_snapshot rather than restoring garbage. Null the column through the
    app's own DB session after a real accept, mirroring _seed_one_pending's
    c.portal.call idiom."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import TriageProposal

    with _client() as c:
        _register(c, "viberpit", repo="o/r")
        pid, _wid = _seed_one_pending(c, feature_slug="sessions")
        assert c.post(f"/api/pm/triage/{pid}/accept",
                      headers=IDENTITY_HEADERS).status_code == 200

        async def _clear_snapshot():
            async with db_session() as db:
                prop = (await db.execute(select(TriageProposal).where(
                    TriageProposal.id == pid))).scalar_one()
                prop.undo_snapshot = None
        c.portal.call(_clear_snapshot)

        r = c.post(f"/api/pm/triage/{pid}/undo", headers=IDENTITY_HEADERS)
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "no_snapshot"


def test_triage_undo_no_snapshot_beats_the_window(monkeypatch):
    """The case the no_snapshot branch actually EXISTS for: a row decided before
    the column was added. Such a row is also, always, outside the undo window —
    so with the window checked first it could only ever have answered
    undo_expired, and no_snapshot documented a reachable-by-nobody state. The
    missing snapshot is the more specific reason; it is the one reported."""
    import server.api.pm as pm_api
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import TriageProposal

    with _client() as c:
        _register(c, "viberpit", repo="o/r")
        pid, _wid = _seed_one_pending(c, feature_slug="sessions")
        assert c.post(f"/api/pm/triage/{pid}/accept",
                      headers=IDENTITY_HEADERS).status_code == 200

        async def _clear_snapshot():
            async with db_session() as db:
                prop = (await db.execute(select(TriageProposal).where(
                    TriageProposal.id == pid))).scalar_one()
                prop.undo_snapshot = None
        c.portal.call(_clear_snapshot)
        # Aged out as well — the pre-migration row's real shape.
        monkeypatch.setattr(pm_api, "TRIAGE_UNDO_WINDOW_S", 0)

        r = c.post(f"/api/pm/triage/{pid}/undo", headers=IDENTITY_HEADERS)
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "no_snapshot", r.text


def test_undo_of_an_auto_accept_marks_the_proposal_auto_undone():
    """Spec §8: a human undid the machine once; the machine never gets a second
    vote. Undo restores the overlay exactly as for a human accept AND flags the
    proposal."""
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import Feature, Project, TriageProposal, WorkItem
    from server.pm.triage_accept import apply_accept

    with _client_with_stubbed_seams() as c:
        _register(c, "Viberpit", repo="o/r")

        async def _seed():
            async with db_session() as db:
                p = (await db.execute(select(Project).where(
                    Project.slug == "viberpit"))).scalar_one()
                f = Feature(project_id=p.id, slug="sessions", title="Sessions", state="live")
                w = WorkItem(repo="o/r", number=1, kind="issue", title="a",
                             state="open", triage_state="proposed")
                db.add_all([f, w])
                await db.flush()
                prop = TriageProposal(work_item_id=w.id, proposed_feature_id=f.id,
                                      confidence_level="high", status="pending")
                db.add(prop)
                await db.flush()
                await apply_accept(db, prop, decided_by="auto", audit_identity="pm-triage", ownership=await ownership_snapshot(db, "o/r"))
                return prop.id
        pid = c.portal.call(_seed)

        r = c.post(f"/api/pm/triage/{pid}/undo", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text

        async def _check():
            async with db_session() as db:
                prop = (await db.execute(select(TriageProposal).where(
                    TriageProposal.id == pid))).scalar_one()
                w = (await db.execute(select(WorkItem).where(WorkItem.number == 1))).scalar_one()
                assert prop.status == "pending" and prop.auto_undone is True
                assert w.triage_state == "proposed" and w.classified_by is None
        c.portal.call(_check)


def test_undo_of_an_auto_accept_a_human_patched_first_still_marks_auto_undone():
    """`auto_undone` must come from the PROPOSAL's own record of who decided it,
    not from ``WorkItem.classified_by`` — that column is live overlay state any
    overlay PATCH rewrites to the caller's actor identity. Score the auto-accepted item inside the
    undo window (the exact thing the chip invites: look, tweak, then undo) and
    the machine's own decision is no longer legible from the work item. Read that
    way, the flag stayed NULL and the loop was free to auto-accept the same
    proposal on its next tick — undoing the human."""
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import Feature, Project, TriageProposal, WorkItem
    from server.pm.triage_accept import apply_accept

    with _client_with_stubbed_seams() as c:
        _register(c, "Viberpit", repo="o/r")

        async def _seed():
            async with db_session() as db:
                p = (await db.execute(select(Project).where(
                    Project.slug == "viberpit"))).scalar_one()
                f = Feature(project_id=p.id, slug="sessions", title="Sessions", state="live")
                w = WorkItem(repo="o/r", number=1, kind="issue", title="a",
                             state="open", triage_state="proposed")
                db.add_all([f, w])
                await db.flush()
                prop = TriageProposal(work_item_id=w.id, proposed_feature_id=f.id,
                                      confidence_level="high", status="pending")
                db.add(prop)
                await db.flush()
                await apply_accept(db, prop, decided_by="auto", audit_identity="pm-triage", ownership=await ownership_snapshot(db, "o/r"))
                return prop.id, w.id
        pid, wid = c.portal.call(_seed)

        patched = c.patch(f"/api/pm/work-items/{wid}/overlay", json={"reach": 5},
                          headers=IDENTITY_HEADERS)
        assert patched.status_code == 200, patched.text
        assert patched.json()["classifiedBy"] == "testuser@example.com"

        async def _snapshot():
            async with db_session() as db:
                return (await db.execute(select(TriageProposal.undo_snapshot).where(
                    TriageProposal.id == pid))).scalars().one()
        snap = c.portal.call(_snapshot)
        # `decidedBy` is the machine's decision, beside the snapshot rather than
        # inside it: `classifiedBy` is the PRE-accept overlay value (nobody had
        # classified the item), which is exactly why it cannot carry provenance.
        assert snap["decidedBy"] == "auto"
        assert snap["classifiedBy"] is None

        r = c.post(f"/api/pm/triage/{pid}/undo", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text

        async def _check():
            async with db_session() as db:
                prop = (await db.execute(select(TriageProposal).where(
                    TriageProposal.id == pid))).scalar_one()
                assert prop.status == "pending" and prop.auto_undone is True
        c.portal.call(_check)


def test_undo_of_a_human_accept_does_not_mark_auto_undone():
    """Mirror of the auto case: undoing a human decision must not flip the flag
    the loop checks — otherwise it would wrongly withhold the machine from a
    proposal it never touched."""
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import Feature, Project, TriageProposal, WorkItem
    from server.pm.triage_accept import apply_accept

    with _client_with_stubbed_seams() as c:
        _register(c, "Viberpit", repo="o/r")

        async def _seed():
            async with db_session() as db:
                p = (await db.execute(select(Project).where(
                    Project.slug == "viberpit"))).scalar_one()
                f = Feature(project_id=p.id, slug="sessions", title="Sessions", state="live")
                w = WorkItem(repo="o/r", number=1, kind="issue", title="a",
                             state="open", triage_state="proposed")
                db.add_all([f, w])
                await db.flush()
                prop = TriageProposal(work_item_id=w.id, proposed_feature_id=f.id,
                                      confidence_level="high", status="pending")
                db.add(prop)
                await db.flush()
                await apply_accept(db, prop, decided_by="human", audit_identity="tester", ownership=await ownership_snapshot(db, "o/r"))
                return prop.id
        pid = c.portal.call(_seed)

        r = c.post(f"/api/pm/triage/{pid}/undo", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text

        async def _check():
            async with db_session() as db:
                prop = (await db.execute(select(TriageProposal).where(
                    TriageProposal.id == pid))).scalar_one()
                assert prop.status == "pending" and prop.auto_undone is None
        c.portal.call(_check)


def test_triage_list_includes_a_fresh_auto_accept_with_its_marker():
    """The inbox chip (spec §8/§9) has nothing to render unless the list carries
    the auto-accepted row for the undo window."""
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import Feature, Project, TriageProposal, WorkItem
    from server.pm.triage_accept import apply_accept

    with _client_with_stubbed_seams() as c:
        _register(c, "Viberpit", repo="o/r")

        async def _seed():
            async with db_session() as db:
                p = (await db.execute(select(Project).where(
                    Project.slug == "viberpit"))).scalar_one()
                f = Feature(project_id=p.id, slug="sessions", title="Sessions", state="live")
                w_pending = WorkItem(repo="o/r", number=1, kind="issue", title="pending",
                                     state="open", triage_state="proposed")
                w_auto = WorkItem(repo="o/r", number=2, kind="issue", title="auto",
                                  state="open", triage_state="proposed")
                w_human = WorkItem(repo="o/r", number=3, kind="issue", title="human",
                                   state="open", triage_state="proposed")
                db.add_all([f, w_pending, w_auto, w_human])
                await db.flush()
                pending = TriageProposal(work_item_id=w_pending.id, proposed_feature_id=f.id,
                                         confidence_level="high", status="pending")
                auto_prop = TriageProposal(work_item_id=w_auto.id, proposed_feature_id=f.id,
                                           confidence_level="high", status="pending")
                human_prop = TriageProposal(work_item_id=w_human.id, proposed_feature_id=f.id,
                                            confidence_level="high", status="pending")
                db.add_all([pending, auto_prop, human_prop])
                await db.flush()
                await apply_accept(db, auto_prop, decided_by="auto", audit_identity="pm-triage", ownership=await ownership_snapshot(db, "o/r"))
                await apply_accept(db, human_prop, decided_by="human", audit_identity="tester", ownership=await ownership_snapshot(db, "o/r"))
                return pending.id, auto_prop.id, human_prop.id
        pending_id, auto_id, human_id = c.portal.call(_seed)

        r = c.get("/api/pm/triage", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        by_id = {row["id"]: row for row in r.json()}
        assert set(by_id) == {pending_id, auto_id}
        assert human_id not in by_id
        assert by_id[pending_id]["status"] == "pending"
        assert by_id[pending_id]["classifiedBy"] is None
        assert by_id[auto_id]["status"] == "accepted"
        assert by_id[auto_id]["classifiedBy"] == "auto"
        assert isinstance(by_id[auto_id]["resolvedAt"], str) and by_id[auto_id]["resolvedAt"]


def test_triage_list_pending_only_omits_a_fresh_auto_accept():
    """``?pendingOnly=true`` is the un-widened list — exactly ``status ==
    "pending"``. Consumers that COUNT the queue (the assistant's list_triage, and
    any caller that wants "awaiting a decision") must not see the undo-window
    rows the inbox renders, or they over-count by every fresh auto-accept."""
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import Feature, Project, TriageProposal, WorkItem
    from server.pm.triage_accept import apply_accept

    with _client_with_stubbed_seams() as c:
        _register(c, "Viberpit", repo="o/r")

        async def _seed():
            async with db_session() as db:
                p = (await db.execute(select(Project).where(
                    Project.slug == "viberpit"))).scalar_one()
                f = Feature(project_id=p.id, slug="sessions", title="Sessions", state="live")
                w_pending = WorkItem(repo="o/r", number=1, kind="issue", title="pending",
                                     state="open", triage_state="proposed")
                w_auto = WorkItem(repo="o/r", number=2, kind="issue", title="auto",
                                  state="open", triage_state="proposed")
                db.add_all([f, w_pending, w_auto])
                await db.flush()
                pending = TriageProposal(work_item_id=w_pending.id, proposed_feature_id=f.id,
                                         confidence_level="high", status="pending")
                auto_prop = TriageProposal(work_item_id=w_auto.id, proposed_feature_id=f.id,
                                           confidence_level="high", status="pending")
                db.add_all([pending, auto_prop])
                await db.flush()
                await apply_accept(db, auto_prop, decided_by="auto", audit_identity="pm-triage", ownership=await ownership_snapshot(db, "o/r"))
                return pending.id, auto_prop.id
        pending_id, auto_id = c.portal.call(_seed)

        widened = c.get("/api/pm/triage", headers=IDENTITY_HEADERS)
        assert {row["id"] for row in widened.json()} == {pending_id, auto_id}

        r = c.get("/api/pm/triage?pendingOnly=true", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        assert [row["id"] for row in r.json()] == [pending_id]


def test_triage_list_drops_an_auto_accept_after_the_undo_window():
    """Once past TRIAGE_UNDO_WINDOW_S, the row leaves the inbox — the same
    boundary undo_triage enforces to reject the undo POST itself."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import Feature, Project, TriageProposal, WorkItem
    from server.pm.triage_accept import apply_accept

    with _client_with_stubbed_seams() as c:
        _register(c, "Viberpit", repo="o/r")

        async def _seed():
            async with db_session() as db:
                p = (await db.execute(select(Project).where(
                    Project.slug == "viberpit"))).scalar_one()
                f = Feature(project_id=p.id, slug="sessions", title="Sessions", state="live")
                w = WorkItem(repo="o/r", number=1, kind="issue", title="a",
                             state="open", triage_state="proposed")
                db.add_all([f, w])
                await db.flush()
                prop = TriageProposal(work_item_id=w.id, proposed_feature_id=f.id,
                                      confidence_level="high", status="pending")
                db.add(prop)
                await db.flush()
                await apply_accept(db, prop, decided_by="auto", audit_identity="pm-triage", ownership=await ownership_snapshot(db, "o/r"))
                return prop.id
        pid = c.portal.call(_seed)

        import server.api.pm as pm_api

        async def _age_out():
            async with db_session() as db:
                prop = (await db.execute(select(TriageProposal).where(
                    TriageProposal.id == pid))).scalar_one()
                prop.resolved_at = (datetime.now(timezone.utc)
                                    - timedelta(seconds=pm_api.TRIAGE_UNDO_WINDOW_S + 5))
        c.portal.call(_age_out)

        r = c.get("/api/pm/triage", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        assert all(row["id"] != pid for row in r.json())


def test_triage_list_keeps_an_auto_accept_a_human_patched_inside_the_window():
    """The inbox chip's row must not vanish when a human scores the leaf inside
    the undo window: the overlay PATCH re-stamps ``WorkItem.classified_by`` as
    human, so the list's provenance is read off the proposal's own snapshot."""
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import Feature, Project, TriageProposal, WorkItem
    from server.pm.triage_accept import DECIDED_BY_AUTO, apply_accept

    with _client_with_stubbed_seams() as c:
        _register(c, "Viberpit", repo="o/r")

        async def _seed():
            async with db_session() as db:
                p = (await db.execute(select(Project).where(
                    Project.slug == "viberpit"))).scalar_one()
                f = Feature(project_id=p.id, slug="sessions", title="Sessions", state="live")
                w = WorkItem(repo="o/r", number=1, kind="issue", title="a",
                             state="open", triage_state="proposed")
                db.add_all([f, w])
                await db.flush()
                prop = TriageProposal(work_item_id=w.id, proposed_feature_id=f.id,
                                      confidence_level="high", status="pending")
                db.add(prop)
                await db.flush()
                await apply_accept(db, prop, decided_by=DECIDED_BY_AUTO,
                                   audit_identity="pm-triage", ownership=await ownership_snapshot(db, "o/r"))
                return prop.id, w.id
        pid, wid = c.portal.call(_seed)

        r = c.patch(f"/api/pm/work-items/{wid}/overlay", json={"reach": 5},
                    headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text

        rows = c.get("/api/pm/triage", headers=IDENTITY_HEADERS).json()
        mine = [x for x in rows if x["id"] == pid]
        assert len(mine) == 1
        assert mine[0]["status"] == "accepted" and mine[0]["classifiedBy"] == "auto"


def test_ambiguous_accept_is_conflict_and_bulk_gathers_once(monkeypatch):
    from unittest.mock import AsyncMock
    from server.automations import target_identity
    from server.db import db_session
    from server.db.models import WorkItem, TriageProposal

    with _client_with_stubbed_seams() as c:
        _register(c, "First", repo="o/r")
        _register(c, "Second", repo="old/r")
        pid, wid = _seed_one_pending(c, feature_slug="sessions", number=1, project_slug="first")
        resolve = AsyncMock(return_value={"o/r": "o/r", "old/r": "o/r"})
        monkeypatch.setattr(target_identity, "resolve_repositories", resolve)
        response = c.post(f"/api/pm/triage/{pid}/accept", headers=IDENTITY_HEADERS)
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["ownershipState"] == "ambiguous"
        resolve.reset_mock()
        response = c.post("/api/pm/triage/accept-bulk", json={"ids": [pid, pid]}, headers=IDENTITY_HEADERS)
        assert response.status_code == 200, response.text
        assert response.json()["accepted"] == 0
        assert all(row["ownershipState"] == "ambiguous" for row in response.json()["results"])
        resolve.assert_awaited_once()

        async def unchanged():
            async with db_session() as db:
                assert (await db.get(TriageProposal, pid)).status == "pending"
                assert (await db.get(WorkItem, wid)).triage_state == "unclassified"
        c.portal.call(unchanged)


def test_policy_patch_requires_explicit_policy_and_refuses_stale_reset_edits():
    with _client() as c:
        c.post("/api/pm/projects", json={"name": "P", "slug": "p", "repos": [{"repo": "owner/repo"}]}, headers=IDENTITY_HEADERS)
        url = "/api/pm/projects/p/policy"
        assert c.patch(url, json={}, headers=IDENTITY_HEADERS).status_code == 422
        first = c.patch(url, json={"policy": {"dispatch": "off"}, "expectedUpdatedAt": None}, headers=IDENTITY_HEADERS)
        assert first.status_code == 200, first.text
        rev = first.json()["automationPolicyUpdatedAt"]
        assert rev
        assert c.patch(url, json={"policy": {"weight": 4}, "expectedUpdatedAt": None}, headers=IDENTITY_HEADERS).status_code == 409
        merged = c.patch(url, json={"policy": {"weight": 4}, "expectedUpdatedAt": rev}, headers=IDENTITY_HEADERS)
        assert merged.status_code == 200, merged.text
        assert merged.json()["automationPolicy"] == {"dispatch": "off", "weight": 4}
        reset = c.patch(url, json={"policy": None, "expectedUpdatedAt": merged.json()["automationPolicyUpdatedAt"]}, headers=IDENTITY_HEADERS)
        assert reset.status_code == 200, reset.text
        assert reset.json()["automationPolicy"] is None
        assert reset.json()["automationPolicyUpdatedAt"] != merged.json()["automationPolicyUpdatedAt"]
        assert c.patch(url, json={"policy": {"dispatch": "auto"}, "expectedUpdatedAt": rev}, headers=IDENTITY_HEADERS).status_code == 409
        # Compatibility callers merge sparsely, never erase unspecified choices.
        c.patch(url, json={"policy": {"dispatch": "off"}}, headers=IDENTITY_HEADERS)
        final = c.patch(url, json={"policy": {"weight": 2}}, headers=IDENTITY_HEADERS)
        assert final.json()["automationPolicy"] == {"dispatch": "off", "weight": 2}


def test_policy_patch_refuses_corrupt_stored_policy_but_allows_explicit_reset():
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import AuditEvent, Project
    with _client() as c:
        async def seed():
            async with db_session() as db:
                db.add(Project(name="Bad", slug="bad", automation_policy={"unknown_grant": True}))
        c.portal.call(seed)
        url = "/api/pm/projects/bad/policy"
        response = c.patch(url, json={"policy": {"weight": 2}}, headers=IDENTITY_HEADERS)
        assert response.status_code == 422, response.text
        async def observed():
            async with db_session() as db:
                p = (await db.scalars(select(Project).where(Project.slug == "bad"))).one()
                audits = (await db.scalars(select(AuditEvent).where(AuditEvent.type == "pm.project.policy"))).all()
                return p.automation_policy, p.automation_policy_updated_at, len(audits)
        assert c.portal.call(observed) == ({"unknown_grant": True}, None, 0)
        response = c.patch(url, json={"policy": None, "expectedUpdatedAt": None}, headers=IDENTITY_HEADERS)
        assert response.status_code == 200, response.text
        assert response.json()["automationPolicy"] is None
        assert response.json()["automationPolicyUpdatedAt"]
        assert c.portal.call(observed)[2] == 1


def test_triage_rows_carry_guidance_from_the_products_dispatch_policy():
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import Feature, Project, TriageProposal, WorkItem

    with _client() as c:
        _register(c, "Viberpit", repo="o/r")

        async def _seed():
            async with db_session() as db:
                p = (await db.execute(select(Project).where(Project.slug == "viberpit"))).scalar_one()
                p.automation_policy = {"dispatch": "auto", "queue_min_status": "Ready"}
                f = Feature(project_id=p.id, slug="sessions", title="Sessions", state="live")
                w = WorkItem(repo="o/r", number=1, kind="issue", title="a", state="open",
                             triage_state="proposed")
                db.add_all([f, w])
                await db.flush()
                db.add(TriageProposal(work_item_id=w.id, proposed_feature_id=f.id,
                                      reach=3, impact=2, effort=1, confidence=2,
                                      rationale="fits", confidence_level="high", status="pending"))
        c.portal.call(_seed)

        row = c.get("/api/pm/triage", headers=IDENTITY_HEADERS).json()[0]
        g = row["guidance"]
        assert g["accept"]["immediate"][0]["text"] == "Files it under sessions with score 12; it leaves the inbox."
        assert g["accept"]["then"][0]["spawnsSession"] is True
        assert "coding session" in g["accept"]["then"][0]["text"]
        assert g["reject"]["when"] == "now"


def test_triage_guidance_for_an_unowned_repo_reads_as_dispatch_off():
    """No product owns the repo → defaults → nothing is dispatched."""
    from server.db import db_session
    from server.db.models import TriageProposal, WorkItem

    with _client() as c:
        async def _seed():
            async with db_session() as db:
                w = WorkItem(repo="nobody/owns", number=1, kind="issue", title="a", state="open",
                             triage_state="proposed")
                db.add(w)
                await db.flush()
                db.add(TriageProposal(work_item_id=w.id, no_fit_suggestion="billing",
                                      rationale="no fit", confidence_level="low", status="pending"))
        c.portal.call(_seed)
        row = c.get("/api/pm/triage", headers=IDENTITY_HEADERS).json()[0]
        assert row["guidance"]["accept"]["immediate"][0]["text"].startswith("Files it with no capability")
        assert "Dispatch is off" in row["guidance"]["accept"]["then"][0]["text"]


def test_hold_release_endpoint_is_the_retry_signal():
    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")
        r = c.patch(f"/api/pm/work-items/{wid}/overlay",
                    json={"holdKind": "owner_action", "holdReason": "owner only"},
                    headers=IDENTITY_HEADERS)
        assert r.status_code == 200
        r = c.post(f"/api/pm/work-items/{wid}/hold/release", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["released"] == "owner_action"
        assert r.json()["item"]["holdKind"] is None
        # idempotent no-op
        again = c.post(f"/api/pm/work-items/{wid}/hold/release", headers=IDENTITY_HEADERS)
        assert again.status_code == 200 and again.json()["released"] is None
        missing = c.post("/api/pm/work-items/nope/hold/release", headers=IDENTITY_HEADERS)
        assert missing.status_code == 404
        from server.db import db_session
        from sqlalchemy import select
        from server.db.models import AuditEvent

        async def _rows():
            async with db_session() as db:
                return [(e.detail, e.via) for e in (await db.execute(
                    select(AuditEvent).where(AuditEvent.type == "pm.hold.release"))).scalars().all()]
        rows = c.portal.call(_rows)
        assert [d["signal"] for d, _ in rows] == ["retry"] and rows[0][0]["actorClass"] == "person"
        assert rows[0][1] == "api"   # same shape as the overlay's release (F5)


def test_hold_release_refuses_a_decision_hold():
    """Spec §8: a decision hold resolves through its proposal, never through the
    Retry signal — Retry would drop the question on the floor and re-queue the item
    with the decision still unmade."""
    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")
        c.patch(f"/api/pm/work-items/{wid}/overlay",
                json={"holdKind": "decision", "holdReason": "which storage engine?"},
                headers=IDENTITY_HEADERS)
        r = c.post(f"/api/pm/work-items/{wid}/hold/release", headers=IDENTITY_HEADERS)
        assert r.status_code == 422 and r.json()["detail"]["error"] == "bad_hold"
        assert "proposal" in r.json()["detail"]["message"]
        from server.db import db_session
        from server.db.models import WorkItem

        async def _kind():
            async with db_session() as db:
                return (await db.get(WorkItem, wid)).hold_kind
        assert c.portal.call(_kind) == "decision"


def test_hold_release_refused_for_agent_class_caller():
    with patch.dict(os.environ, LAN_OPEN), _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")
        c.patch(f"/api/pm/work-items/{wid}/overlay",
                json={"holdKind": "owner_action", "holdReason": "owner only"},
                headers=IDENTITY_HEADERS)
        r = c.post(f"/api/pm/work-items/{wid}/hold/release")   # lan@local, no header
        assert r.status_code == 422 and r.json()["detail"]["error"] == "hold_not_permitted"


def test_reconcile_save_releases_hold_on_human_board_edit():
    """The reconciler's project-items save runs THE release rule (spec §7)."""
    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")
        c.patch(f"/api/pm/work-items/{wid}/overlay",
                json={"holdKind": "owner_action", "holdReason": "owner only"},
                headers=IDENTITY_HEADERS)
        from server.db import db_session
        from sqlalchemy import select
        from server.db.models import GhProject, GhProjectItem, Project, WorkItem
        from server.pm.gh_projects import GhProjectItemView

        async def _seed_board():
            async with db_session() as db:
                p = (await db.execute(select(Project).where(Project.slug == "viberpit"))).scalar_one()
                gh = GhProject(project_id=p.id, gh_node_id="PVT_1", gh_project_number=1, title="b")
                db.add(gh)
                await db.flush()
                db.add(GhProjectItem(gh_project_id=gh.id, work_item_id=wid,
                                     gh_item_node_id="PVTI_1", status="Needs human"))
                return gh.id
        gh_id = c.portal.call(_seed_board)
        orch = c.app.state.orchestrator
        view = GhProjectItemView(item_node_id="PVTI_1", repo="o/r", number=1, kind="issue",
                                 status="Ready", score=None, stage=None, updated_at=None)
        edits = c.portal.call(lambda: orch._pm_sync_save_project_items(gh_id, [view]))
        assert [(e.field, e.new) for e in edits] == [("Status", "Ready")]

        async def _item():
            async with db_session() as db:
                return (await db.execute(select(WorkItem).where(WorkItem.id == wid))).scalar_one().hold_kind
        assert c.portal.call(_item) is None


def test_issue_detail_carries_hold_fields_and_guidance():
    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")
        r = c.get(f"/api/pm/work-items/{wid}/issue", headers=IDENTITY_HEADERS)
        assert r.status_code == 200
        assert r.json()["holdKind"] is None and r.json()["guidance"] is None
        c.patch(f"/api/pm/work-items/{wid}/overlay",
                json={"holdKind": "owner_action", "holdReason": "needs the owner's keyring"},
                headers=IDENTITY_HEADERS)
        r = c.get(f"/api/pm/work-items/{wid}/issue", headers=IDENTITY_HEADERS)
        body = r.json()
        assert body["holdKind"] == "owner_action" and body["holdReason"] == "needs the owner's keyring"
        assert body["holdBy"] == "testuser@example.com" and body["holdSetAt"]
        assert set(body["guidance"]) == {"retry", "implement"}
        assert body["ref"] == "o/r#1"
        # the hold is not part of the optimistic-concurrency version
        assert body["version"] == c.get(f"/api/pm/work-items/{wid}/issue", headers=IDENTITY_HEADERS).json()["version"]


def test_issue_detail_reads_the_policy_only_for_an_owner_action_hold():
    """`hold_guidance` decides which kinds carry copy; the endpoint just supplies a
    policy for whichever kind is on the row. Its evidence copy is policy-independent
    (spec 2026-09-18 §5), so an evidence hold must not pay for the owning-product
    lookup / policy load an owner_action hold needs — the common unheld-issue case
    pays for neither."""
    from unittest.mock import AsyncMock
    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")
        c.patch(f"/api/pm/work-items/{wid}/overlay",
                json={"holdKind": "evidence", "holdReason": "needs a repro"},
                headers=IDENTITY_HEADERS)
        loader = AsyncMock(name="load_policies")
        with patch("server.api.pm.load_policies", loader):
            r = c.get(f"/api/pm/work-items/{wid}/issue", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["holdKind"] == "evidence"
        assert set(r.json()["guidance"]) == {"evidence"}
        loader.assert_not_awaited()


def test_issue_detail_guidance_for_evidence_and_none_for_stronger_model():
    """spec 2026-09-18 §5: an evidence hold's live detail response carries the
    single button-less consequence `hold_guidance` computes for it (the API gap
    task 6's own review caught — `guidance` must not stay null in production for
    this kind); `stronger_model`, which `hold_guidance` does not know, still reads
    as no guidance at all."""
    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")
        c.patch(f"/api/pm/work-items/{wid}/overlay",
                json={"holdKind": "evidence", "holdReason": "auto-filed error report"},
                headers=IDENTITY_HEADERS)
        r = c.get(f"/api/pm/work-items/{wid}/issue", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["holdKind"] == "evidence"
        assert set(body["guidance"]) == {"evidence"}
        e = body["guidance"]["evidence"]
        assert e["available"] is True
        assert "investigates this report instead of implementing it" in e["then"][0]["text"]
        assert e["immediate"] == []

        c.patch(f"/api/pm/work-items/{wid}/overlay",
                json={"holdKind": "stronger_model", "holdReason": "bounced at sonnet"},
                headers=IDENTITY_HEADERS)
        r = c.get(f"/api/pm/work-items/{wid}/issue", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["holdKind"] == "stronger_model" and r.json()["guidance"] is None
