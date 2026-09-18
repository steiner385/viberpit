"""PM global priority queue API tests — GET /api/pm/queue.

Mirrors test_pm_deps_api.py fixture pattern: _client() context manager, async ORM
seeding via db_session(), CCSWITCH_DEV=1 + empty allowlist auth bypass.
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


def _seed_ready_and_blocked(c) -> None:
    """One onboarded product with a linked board: a Ready item and a Blocked-by-dep
    item (an active native dep whose destination issue is open).

    Seeded through the app's OWN event loop (same idiom as
    test_cicd_findings_api._seed). A bare ``asyncio.run(_seed_async())`` runs on a
    throwaway loop while the app holds the module-level ``server.db._engine`` on the
    TestClient portal loop. Both share one AsyncAdaptedQueuePool, and that pool's
    ``asyncio.Queue`` binds to whichever loop first *awaits* a checkout —
    ``get_nowait()`` never checks loop affinity, which is why an idle pool tolerates
    the cross-loop seed. Once the orchestrator's control loops saturate the
    5-connection pool at startup, the foreign-loop checkout has to await and dies
    with "Queue ... is bound to a different event loop".
    """
    from server.db import db_session
    from server.db.models import (
        GhProject, GhProjectItem, Project, ProjectRepo, WorkItem, WorkItemDep)

    async def _seed_async() -> None:
        async with db_session() as db:
            proj = Project(slug="viberpit", name="viberpit")
            db.add(proj)
            await db.flush()

            db.add(ProjectRepo(project_id=proj.id,
                               repo="steiner385/viberpit",
                               is_primary=True))

            wi_ready = WorkItem(repo="steiner385/viberpit", number=100,
                                kind="issue", title="ready item", state="open",
                                body="do the thing", url="https://example.com/100")
            wi_blocked = WorkItem(repo="steiner385/viberpit", number=101,
                                  kind="issue", title="blocked item", state="open",
                                  body="blocked thing", url="https://example.com/101")
            wi_blocker = WorkItem(repo="steiner385/viberpit", number=102,
                                  kind="issue", title="blocker item", state="open")
            db.add_all([wi_ready, wi_blocked, wi_blocker])
            await db.flush()

            ghp = GhProject(project_id=proj.id, gh_project_number=7,
                            title="viberpit")
            db.add(ghp)
            await db.flush()

            db.add(GhProjectItem(gh_project_id=ghp.id, work_item_id=wi_ready.id,
                                 status="Ready", score=5.0))
            db.add(GhProjectItem(gh_project_id=ghp.id, work_item_id=wi_blocked.id,
                                 status="Ready", score=9.0))

            # Active native dep: blocked -> blocker (blocker is open) => blocked-by-dep.
            db.add(WorkItemDep(
                src_ref="steiner385/viberpit#101",
                dst_ref="steiner385/viberpit#102",
                source="native",
                active=True,
            ))
            await db.commit()

    assert c.portal is not None
    c.portal.call(_seed_async)


def test_queue_endpoint_ranks_ready_and_skips_blocked():
    with _client() as c:
        _seed_ready_and_blocked(c)

        resp = c.get("/api/pm/queue", headers=IDENTITY_HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert [e["ref"] for e in body["entries"]] == ["steiner385/viberpit#100"]
        entry = body["entries"][0]
        assert entry["repo"] == "steiner385/viberpit"
        assert entry["number"] == 100
        assert entry["product"] == "viberpit"
        assert entry["title"] == "ready item"
        assert entry["url"] == "https://example.com/100"
        assert entry["score"] == 5.0
        assert "body" not in entry

        assert body["skipped"] == {"blocked": 1}
        assert body["generatedAt"]


def _seed_runner_row(c, target: str, state: dict) -> None:
    """One durable backlog-runner target-state row, through the app's own loop.

    A CLAIM row seeded here MUST carry a ``session_id``. ``_client()`` starts the
    real orchestrator, whose ``_backlog_tick_loop`` ticks ONCE IMMEDIATELY at
    startup (``while True: tick(); sleep(60)``) and runs concurrently with the
    test body. That tick's ``_reconcile_dangling`` deletes every ``claimed`` row
    with no ``session_id`` — by the runner's own definition that shape is the
    crash-between-claim-and-spawn leak, not a held claim. A claim seeded without
    one is garbage the runner is designed to collect, so the assertion below
    passes or fails on whether the tick happened to win the race (it usually
    loses locally and wins on a loaded CI runner). A ``gave_up`` row needs no
    such guard: the reconciler skips it, and it parks nothing by itself — the
    hold on the ITEM is what parks the issue (spec 2026-09-18 §3). The row
    survives only for slot accounting, and ``_release_give_ups`` drops it on the
    next tick once none of its refs is held any more.

    That ``session_id`` must name a session row that EXISTS and is not terminal,
    so this seeds one alongside. A claim the runner holds without an ownership
    token is a pre-ownership row, and the sweep now discharges one whose session
    is gone (``_release_legacy_claim``) — exactly the dead-slot shape that stalled
    the runner for 13 hours. A dangling id would therefore make this fixture a
    phantom claim the runner is right to collect, not the held claim it means.
    """
    from server.db import db_session
    from server.db.models import Automation, AutomationTargetState, Session as DbSession

    async def _seed_async() -> None:
        async with db_session() as db:
            if await db.get(Automation, "backlog-runner") is None:
                db.add(Automation(id="backlog-runner"))
                await db.flush()
            sid = state.get("session_id")
            if sid and await db.get(DbSession, sid) is None:
                db.add(DbSession(id=sid, name=f"runner {target}", account_name="acct",
                                 cwd="/tmp", status="working"))
                await db.flush()
            db.add(AutomationTargetState(automation_id="backlog-runner",
                                         target=target, state=state))
            await db.commit()

    assert c.portal is not None
    c.portal.call(_seed_async)


def _set_hold(c, repo: str, number: int, *, kind: str = "owner_action") -> None:
    """Stamp a hold directly on the item — spec 2026-09-18 §3: the item's own
    ``hold_kind`` is the queue's parked signal now, not a runner give-up row."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import WorkItem

    async def _set_async() -> None:
        async with db_session() as db:
            wi = (await db.execute(select(WorkItem).where(
                WorkItem.repo == repo, WorkItem.number == number))).scalar_one()
            wi.hold_kind, wi.hold_by, wi.hold_reason = kind, "tony@example.com", "max attempts"
            await db.commit()

    assert c.portal is not None
    c.portal.call(_set_async)


def test_queue_endpoint_reads_the_runners_claims_and_a_held_item():
    """The operator's queue view is the runner's pool: an issue the runner holds is
    ``claimed`` here too, and a held item is parked — not quietly ranked as if it
    were up for grabs (ruling D-R1)."""
    with _client() as c:
        _seed_ready_and_blocked(c)
        _set_hold(c, "steiner385/viberpit", 100)
        # ``session_id`` is what makes this a HELD claim rather than a dangler the
        # startup tick reclaims — see _seed_runner_row's docstring.
        _seed_runner_row(c, "steiner385/viberpit#101",
                         {"claimed": True, "session_id": "sess-101",
                          "issues": ["steiner385/viberpit#101"]})

        # ``respect_dependencies=false`` so #101 reports its CLAIM rather than the
        # open dep it also has (the dep gate runs first).
        body = c.get("/api/pm/queue?respect_dependencies=false",
                     headers=IDENTITY_HEADERS).json()

        assert body["entries"] == []
        assert body["skipped"] == {"held": 1, "claimed": 1}


def test_queue_reads_a_held_claim_after_a_runner_tick():
    """Pins the race the test above only sidesteps: a HELD claim (one with a
    ``session_id``) is still reported as ``claimed`` after the backlog runner has
    actually ticked. ``_client()`` starts the real orchestrator, so a tick fires
    on its own schedule anyway — here we drive one deliberately, between the seed
    and the read, so the interleaving CI produces under load is the one asserted.
    Without the ``session_id`` this read leaks #101 into ``entries``."""
    with _client() as c:
        _seed_ready_and_blocked(c)
        _set_hold(c, "steiner385/viberpit", 100)
        _seed_runner_row(c, "steiner385/viberpit#101",
                         {"claimed": True, "session_id": "sess-101",
                          "issues": ["steiner385/viberpit#101"]})

        assert c.portal is not None
        c.portal.call(c.app.state.sessions._orch._backlog_tick_once)

        body = c.get("/api/pm/queue?respect_dependencies=false",
                     headers=IDENTITY_HEADERS).json()

        assert body["entries"] == []
        assert body["skipped"] == {"held": 1, "claimed": 1}


def test_queue_endpoint_invalid_min_status_422():
    with _client() as c:
        resp = c.get("/api/pm/queue?min_status=Nope", headers=IDENTITY_HEADERS)
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["error"] == "invalid_min_status"


def test_queue_endpoint_requires_identity():
    with _client() as c:
        _seed_ready_and_blocked(c)
        assert c.get("/api/pm/queue").status_code == 401
        assert c.get("/api/pm/queue", headers=IDENTITY_HEADERS).status_code == 200


def _seed_bounced_issue(c, *, give_up_after_bounces: int) -> None:
    """One onboarded product with a linked board whose single Ready issue already
    has two FINALISED non-merged dispatches against its CURRENT body."""
    from server.db import db_session
    from server.db.models import (
        DispatchOutcome, GhProject, GhProjectItem, Project, ProjectRepo, WorkItem)
    from server.pm.outcomes import body_fingerprint

    async def _seed_async() -> None:
        async with db_session() as db:
            proj = Project(slug="bouncy", name="bouncy",
                           automation_policy={
                               "give_up_after_bounces": give_up_after_bounces})
            db.add(proj)
            await db.flush()
            db.add(ProjectRepo(project_id=proj.id, repo="o/bouncy", is_primary=True))
            wi = WorkItem(repo="o/bouncy", number=1, kind="issue", title="bounced",
                          state="open", body="needs a spec",
                          url="https://example.com/b1")
            db.add(wi)
            await db.flush()
            ghp = GhProject(project_id=proj.id, gh_project_number=9, title="bouncy")
            db.add(ghp)
            await db.flush()
            db.add(GhProjectItem(gh_project_id=ghp.id, work_item_id=wi.id,
                                 status="Ready", score=3.0))
            fp = body_fingerprint("bounced", "needs a spec")
            db.add_all([
                DispatchOutcome(target_ref="o/bouncy#1", body_fingerprint=fp,
                                outcome="no_pr"),
                DispatchOutcome(target_ref="o/bouncy#1", body_fingerprint=fp,
                                outcome="no_pr"),
            ])
            await db.commit()

    assert c.portal is not None
    c.portal.call(_seed_async)


def test_queue_skips_an_issue_bounced_out_by_its_products_threshold():
    """D8 parity with the runner: this view ranks PER PRODUCT with that product's
    ``give_up_after_bounces``, so an issue the runner would skip as ``needs_spec``
    is not shown as "up next" here either (R3-12)."""
    with _client() as c:
        _seed_bounced_issue(c, give_up_after_bounces=2)

        body = c.get("/api/pm/queue", headers=IDENTITY_HEADERS).json()

        assert body["entries"] == []
        assert body["skipped"] == {"needs_spec": 1}


def test_queue_keeps_a_bounced_issue_for_a_product_that_never_gives_up():
    """The threshold is the PRODUCT's, not a constant: 0 means never give up, and
    the same two bounces then leave the issue rankable."""
    with _client() as c:
        _seed_bounced_issue(c, give_up_after_bounces=0)

        body = c.get("/api/pm/queue", headers=IDENTITY_HEADERS).json()

        assert [e["ref"] for e in body["entries"]] == ["o/bouncy#1"]
        assert body["skipped"] == {}
