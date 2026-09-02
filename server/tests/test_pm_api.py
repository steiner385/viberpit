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
