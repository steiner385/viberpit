"""PM registry API tests (spec §7, phase-1 routes).

One narrated flow (create → list → sync-now → edges) plus edge cases, in the
style of test_sdlc_api.py — reuse that module's fixtures.
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

import asyncio  # noqa: E402 — appended after the phase-1 block


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


def _seed_map_merged(slug):
    """Drive a project to map_merged with a feature + open unclassified items."""
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
    asyncio.run(_run())


def test_onboarding_state_reflects_engine():
    with _client_with_stubbed_seams() as c:
        _register(c, "Ccswitch", repo="o/r")
        r = c.get("/api/pm/projects/ccswitch/onboarding", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        body = r.json()
        # Registered project with a repo but no backfill → await_backfill.
        assert body["state"] == "registered"
        assert body["action"]["type"] == "await_backfill"
        assert body["counts"]["repoCount"] == 1


def test_propose_map_conflicts_when_backfill_incomplete():
    with _client_with_stubbed_seams() as c:
        _register(c, "Ccswitch", repo="o/r")
        r = c.post("/api/pm/projects/ccswitch/propose-map", headers=IDENTITY_HEADERS)
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "backfill_incomplete"


def test_classify_409_when_not_eligible():
    """Classify guard: 409 with error=map_not_merged when state is 'registered'."""
    with _client_with_stubbed_seams() as c:
        _register(c, "Ccswitch", repo="o/r")
        r = c.post("/api/pm/projects/ccswitch/classify", headers=IDENTITY_HEADERS)
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "map_not_merged"


def test_advance_map_transitions_state():
    """advance-map: 200 in map_proposed → onboardingState becomes map_merged;
    409 when state is wrong; classify becomes available after."""
    import asyncio
    from server.db import db_session
    from sqlalchemy import select
    from server.db.models import Project, ProjectRepo

    with _client_with_stubbed_seams() as c:
        _register(c, "Ccswitch", repo="o/r")

        # Must be in map_proposed — 409 when still registered
        r = c.post("/api/pm/projects/ccswitch/advance-map", headers=IDENTITY_HEADERS)
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "not_map_proposed"

        # Force state to map_proposed + backfill_done
        async def _set_proposed():
            async with db_session() as db:
                p = (await db.execute(select(Project).where(
                    Project.slug == "ccswitch"))).scalar_one()
                p.onboarding_state = "map_proposed"
                (await db.execute(select(ProjectRepo).where(
                    ProjectRepo.project_id == p.id))).scalar_one().backfill_done = True
        asyncio.run(_set_proposed())

        # advance succeeds
        r = c.post("/api/pm/projects/ccswitch/advance-map", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["onboardingState"] == "map_merged"

        # DB reflects the new state
        async def _check():
            async with db_session() as db:
                p = (await db.execute(select(Project).where(
                    Project.slug == "ccswitch"))).scalar_one()
                assert p.onboarding_state == "map_merged"
        asyncio.run(_check())


def test_classify_dispatches_and_is_idempotent():
    # Full narrated flow: register → force map_merged → classify → accept.
    with _client_with_stubbed_seams() as c:
        _register(c, "Ccswitch", repo="o/r")
        _seed_map_merged("ccswitch")

        r = c.post("/api/pm/projects/ccswitch/classify", headers=IDENTITY_HEADERS)
        assert r.status_code == 202, r.text
        assert r.json()["dispatched"] == 3


def test_triage_accept_and_reject_flow():
    from server.db import db_session
    from sqlalchemy import select
    from server.db.models import (
        Feature, Project, TriageProposal, WorkItem)

    with _client_with_stubbed_seams() as c:
        _register(c, "Ccswitch", repo="o/r")

        async def _seed_props():
            async with db_session() as db:
                p = (await db.execute(select(Project).where(
                    Project.slug == "ccswitch"))).scalar_one()
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
        asyncio.run(_seed_props())

        listed = c.get("/api/pm/triage", headers=IDENTITY_HEADERS)
        assert listed.status_code == 200, listed.text
        items = listed.json()
        assert len(items) == 2
        accept_id = next(i["id"] for i in items if i["proposedFeatureSlug"] == "sessions")
        reject_id = next(i["id"] for i in items if i["noFitSuggestion"] == "billing")

        # accept copies onto the overlay
        r = c.post(f"/api/pm/triage/{accept_id}/accept", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text

        # reject moves the item to needs_human
        r = c.post(f"/api/pm/triage/{reject_id}/reject", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text

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
                assert w2.triage_state == "needs_human"
        asyncio.run(_check())


def test_triage_accept_applies_edits_body():
    """An optional {edits} body overrides copied overlay values (the shared
    _apply_accept helper); this is what phase-3 edit-then-accept relies on."""
    from server.db import db_session
    from sqlalchemy import select
    from server.db.models import (
        Feature, Project, TriageProposal, WorkItem)

    with _client_with_stubbed_seams() as c:
        _register(c, "Ccswitch", repo="o/r")

        async def _seed_one():
            async with db_session() as db:
                p = (await db.execute(select(Project).where(
                    Project.slug == "ccswitch"))).scalar_one()
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
        asyncio.run(_seed_one())

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
        asyncio.run(_check())


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
        "map_pr_open": False,
        "map_pr_merged": False,
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
    assert view.map_pr_open is False


# ---------------------------------------------------------------------------
# Phase-3 tests: accept-bulk, overlay PATCH, drift re-propose, projectSlug
# ---------------------------------------------------------------------------


def _seed_one_pending(*, feature_slug=None, no_fit_suggestion=None, repo="o/r",
                      number=1, project_slug="ccswitch"):
    """Seed (optionally) a Feature plus a WorkItem and a pending TriageProposal
    against the already-registered project. Returns (proposalId, workItemId)."""
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
    return asyncio.run(_run())


def test_accept_bulk_copies_overlay():
    """accept-bulk runs the SAME copy-onto-overlay path as single-accept and
    reports per-id outcomes in a 200 aggregate."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import WorkItem

    with _client() as c:
        _register(c, "Ccswitch", repo="o/r")
        pid, wid = _seed_one_pending(feature_slug="sessions")

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
        asyncio.run(_check())

        # the accepted proposal has left the pending queue
        listed = c.get("/api/pm/triage", headers=IDENTITY_HEADERS)
        assert listed.status_code == 200
        assert [i["id"] for i in listed.json()] == []


def test_overlay_patch_manual_classify():
    with _client() as c:
        _register(c, "Ccswitch", repo="o/r")
        _pid, wid = _seed_one_pending(feature_slug="sessions")

        r = c.patch(f"/api/pm/work-items/{wid}/overlay",
                    json={"reach": 5, "triageState": "classified"},
                    headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["reach"] == 5
        assert body["triageState"] == "classified"
        assert body["classifiedBy"] == "human"
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


def test_drift_accept_reproposes_orphaned_siblings():
    """Two no_fit items share a suggestion; the drift map merged a matching
    Feature. Accepting one links it AND re-proposes the sibling against the new
    feature as a fresh pending proposal (design §6.3)."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import Feature, Project, TriageProposal, WorkItem

    with _client() as c:
        _register(c, "Ccswitch", repo="o/r")

        async def _seed():
            async with db_session() as db:
                p = (await db.execute(select(Project).where(
                    Project.slug == "ccswitch"))).scalar_one()
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
        (pid1, wid1), (pid2, _wid2) = asyncio.run(_seed())

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
        status_, fid, no_fit, accepted_fid = asyncio.run(_check())
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
        _register(c, "Ccswitch", repo="o/r")

        async def _seed():
            async with db_session() as db:
                p = (await db.execute(select(Project).where(
                    Project.slug == "ccswitch"))).scalar_one()
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
        (pid1, wid1), (pid2, wid2) = asyncio.run(_seed())

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
        by_id = asyncio.run(_check())
        assert by_id[wid1] is not None
        assert by_id[wid1] == by_id[wid2]


def test_drift_accept_via_single_accept_route():
    """The drift branch lives in the shared ``_apply_accept`` helper, so plain
    single-accept inherits it: a no_fit whose suggestion is now a real feature
    links to that feature and re-proposes its siblings."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import Feature, Project, TriageProposal, WorkItem

    with _client() as c:
        _register(c, "Ccswitch", repo="o/r")

        async def _seed():
            async with db_session() as db:
                p = (await db.execute(select(Project).where(
                    Project.slug == "ccswitch"))).scalar_one()
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
        (pid1, wid1), (pid2, _wid2) = asyncio.run(_seed())

        r = c.post(f"/api/pm/triage/{pid1}/accept", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text

        async def _check():
            async with db_session() as db:
                w = (await db.execute(select(WorkItem).where(
                    WorkItem.id == wid1))).scalar_one()
                sib = (await db.execute(select(TriageProposal).where(
                    TriageProposal.id == pid2))).scalar_one()
                return w.feature_id, sib.proposed_feature_id, sib.no_fit_suggestion
        fid, sib_fid, sib_no_fit = asyncio.run(_check())
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
        seeded = asyncio.run(_seed())

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
        fid, sib_fid, sib_no_fit = asyncio.run(_check())
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
        _register(c, "Ccswitch", repo="o/r")
        pid1, wid1 = _seed_one_pending(feature_slug="sessions", number=1)
        pid2, wid2 = _seed_one_pending(feature_slug="queue", number=2)

        real_apply = pm_api._apply_accept

        async def _flaky(db, proposal, identity, *, edits=None):
            # Mutate first, THEN blow up: proves the savepoint discards this id's
            # partial work while leaving the sibling ids' work committed.
            await real_apply(db, proposal, identity, edits=edits)
            if proposal.id == pid2:
                raise RuntimeError("boom")

        with patch.object(pm_api, "_apply_accept", _flaky):
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
        first, second = asyncio.run(_check())
        assert first == ("classified", "accepted")      # survived the sibling's failure
        assert second == ("unclassified", "pending")    # rolled back to itself only


def test_overlay_patch_rejects_out_of_range_rice_values():
    with _client() as c:
        _register(c, "Ccswitch", repo="o/r")
        _pid, wid = _seed_one_pending(feature_slug="sessions")

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
        _register(c, "Ccswitch", repo="o/r")
        _pid, wid = _seed_one_pending(feature_slug="sessions")

        async def _mark_ai():
            async with db_session() as db:
                w = (await db.execute(select(WorkItem).where(
                    WorkItem.id == wid))).scalar_one()
                w.classified_by = "ai"
                w.classified_at = None
        asyncio.run(_mark_ai())

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
        by, at, n_audit = asyncio.run(_check())
        assert (by, at, n_audit) == ("ai", None, 0)


def test_triage_rows_carry_project_slug():
    """The inbox header groups by project, so every row resolves its project
    server-side via ProjectRepo.repo → Project.slug (the client must not guess)."""
    with _client() as c:
        _register(c, "Ccswitch", repo="o/r")
        _seed_one_pending(feature_slug="sessions", repo="o/r", number=1)
        # an item whose repo belongs to no project → projectSlug is null
        _seed_one_pending(feature_slug=None, repo="other/unowned", number=7)

        rows = c.get("/api/pm/triage", headers=IDENTITY_HEADERS)
        assert rows.status_code == 200, rows.text
        by_repo = {row["repo"]: row for row in rows.json()}
        assert by_repo["o/r"]["projectSlug"] == "ccswitch"
        assert by_repo["other/unowned"]["projectSlug"] is None

        # the projectSlug filter still narrows the list
        filtered = c.get("/api/pm/triage?projectSlug=ccswitch",
                         headers=IDENTITY_HEADERS)
        assert filtered.status_code == 200
        assert [row["repo"] for row in filtered.json()] == ["o/r"]
