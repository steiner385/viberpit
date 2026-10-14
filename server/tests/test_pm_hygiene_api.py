"""Needs-you burden API + the hygiene executor's writes (design 2026-10-14 §2):
recommendations (list / apply with confirm / dismiss), the needs-you history, and the
orchestrator seams the loop runs on (load, samples)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from server.tests.test_pm_deps_api import (IDENTITY_HEADERS, _active_pairs, _client,
                                           _seed_stale_fixture)

REPO = "steiner385/viberpit"


def _call(c, fn):
    assert c.portal is not None
    return c.portal.call(fn)


def _seed_recommendations(c, ids: dict[str, str]) -> dict[str, str]:
    """A runner hold on #5 the queue skips, recommended for release; the prunable
    #1 <- #10 edge, recommended for pruning; one already dismissed."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import OwnerRecommendation, Project, WorkItem

    async def _seed() -> dict[str, str]:
        async with db_session() as db:
            pid = (await db.execute(select(Project.id))).scalar_one()
            w5 = await db.get(WorkItem, ids[f"{REPO}#5"])
            assert w5 is not None
            w5.hold_kind, w5.hold_by, w5.hold_reason = "owner_action", "runner", "bounced"
            recs = {
                "release": OwnerRecommendation(
                    project_id=pid, rule="stale_holds", target_ref=f"{REPO}#5",
                    work_item_id=w5.id, action="release_hold", detail={"holdBy": "runner"},
                    evidence="held by runner, skipped anyway"),
                "prune": OwnerRecommendation(
                    project_id=pid, rule="resolved_edges", target_ref=f"{REPO}#1<-{REPO}#10",
                    work_item_id=ids[f"{REPO}#1"], action="prune_edge",
                    detail={"src": f"{REPO}#1", "blocker": f"{REPO}#10"}, evidence="closed"),
                "gone": OwnerRecommendation(
                    project_id=pid, rule="done_but_open", target_ref=f"{REPO}#3",
                    action="close", status="dismissed"),
            }
            db.add_all(recs.values())
            await db.commit()
            return {k: r.id for k, r in recs.items()}
    return _call(c, _seed)


def _audit_rows(c, type_: str) -> list:
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import AuditEvent

    async def _read():
        async with db_session() as db:
            return [(a.identity, a.via, a.detail) for a in (await db.execute(
                select(AuditEvent).where(AuditEvent.type == type_)
                .order_by(AuditEvent.at))).scalars().all()]
    return _call(c, _read)


def test_recommendations_list_apply_with_confirm_and_dismiss():
    with _client() as c:
        ids = _seed_stale_fixture(c)
        recs = _seed_recommendations(c, ids)
        listed = c.get("/api/pm/recommendations?product=viberpit",
                       headers=IDENTITY_HEADERS).json()["recommendations"]
        assert {r["id"] for r in listed} == {recs["release"], recs["prune"]}
        assert {r["product"] for r in listed} == {"viberpit"}
        dismissed = c.get("/api/pm/recommendations?status=dismissed",
                          headers=IDENTITY_HEADERS).json()["recommendations"]
        assert [r["id"] for r in dismissed] == [recs["gone"]]
        assert c.get("/api/pm/recommendations?status=bogus",
                     headers=IDENTITY_HEADERS).status_code == 422

        body = {"ids": [recs["release"], recs["prune"], "missing", recs["gone"]]}
        # Every apply writes to GitHub: the caller must confirm.
        assert c.post("/api/pm/recommendations/apply", headers=IDENTITY_HEADERS,
                      json=body).status_code == 422

        calls: list[tuple[str, str]] = []

        async def fake(work_item_id: str, blocker_ref: str) -> dict:
            calls.append((work_item_id, blocker_ref))
            return {"ok": True}
        c.app.state.sessions.pm_remove_dependency = fake
        out = c.post("/api/pm/recommendations/apply", headers=IDENTITY_HEADERS,
                     json={**body, "confirm": True}).json()
        assert [(r["id"], r["ok"], r["step"]) for r in out["results"]] == [
            (recs["release"], True, None), (recs["prune"], True, None),
            ("missing", False, "not_found"), (recs["gone"], False, "not_pending")]
        assert out["applied"] == 2 and out["failed"] == 2
        assert calls == [(ids[f"{REPO}#1"], f"{REPO}#10")]
        assert (f"{REPO}#1", f"{REPO}#10") in _active_pairs(c)   # the fake seam wrote nothing

        # The hold went, released AS the person through the shared policy-skip rule.
        releases = _audit_rows(c, "pm.hold.release")
        assert [(i, v, d["signal"]) for i, v, d in releases] == [
            ("testuser@example.com", "api", "policy_skip")]
        hygiene = _audit_rows(c, "pm.hygiene.stale_holds") + _audit_rows(
            c, "pm.hygiene.resolved_edges")
        assert [(i, v, d["ok"]) for i, v, d in hygiene] == [
            ("testuser@example.com", "api", True)] * 2
        assert c.get("/api/pm/recommendations", headers=IDENTITY_HEADERS).json()[
            "recommendations"] == []
        applied = c.get("/api/pm/recommendations?status=applied",
                        headers=IDENTITY_HEADERS).json()["recommendations"]
        assert {r["resolvedBy"] for r in applied} == {"testuser@example.com"}

        # Applying twice is refused, not repeated.
        again = c.post("/api/pm/recommendations/apply", headers=IDENTITY_HEADERS,
                       json={"ids": [recs["release"]], "confirm": True}).json()
        assert again["results"][0]["step"] == "not_pending"


def test_dismiss_writes_nothing_to_github_and_sticks():
    with _client() as c:
        ids = _seed_stale_fixture(c)
        recs = _seed_recommendations(c, ids)
        out = c.post("/api/pm/recommendations/dismiss", headers=IDENTITY_HEADERS,
                     json={"ids": [recs["prune"], recs["gone"]]}).json()
        assert [(r["ok"], r["step"]) for r in out["results"]] == [(True, None),
                                                                  (False, "not_pending")]
        assert (f"{REPO}#1", f"{REPO}#10") in _active_pairs(c)
        assert [d["id"] for _, _, d in _audit_rows(c, "pm.hygiene.dismiss")] == [recs["prune"]]

        # The loop never re-recommends what the owner dismissed.
        from server.automations.needs_you_hygiene import upsert_recommendation
        from server.db import db_session
        from server.pm.hygiene import HygieneAction

        async def _again():
            async with db_session() as db:
                from sqlalchemy import select

                from server.db.models import Project
                pid = (await db.execute(select(Project.id))).scalar_one()
                return await upsert_recommendation(db, pid, HygieneAction(
                    "resolved_edges", "viberpit", f"{REPO}#1<-{REPO}#10", ids[f"{REPO}#1"],
                    "prune_edge"))
        assert _call(c, _again) is False


def test_attention_history_serves_the_window_oldest_first():
    from server.db import db_session
    from server.db.models import AttentionSample, Project

    now = datetime.now(timezone.utc)
    with _client() as c:
        async def _seed():
            async with db_session() as db:
                p = Project(slug="viberpit", name="viberpit")
                db.add(p)
                await db.flush()
                for age, n in ((timedelta(days=40), 9), (timedelta(hours=2), 4),
                               (timedelta(hours=1), 3)):
                    db.add(AttentionSample(project_id=p.id, sampled_at=now - age,
                                           needs_you=n, waiting={"blocked": n}))
                await db.commit()
        _call(c, _seed)
        out = c.get("/api/pm/attention/history?product=viberpit&days=30",
                    headers=IDENTITY_HEADERS).json()
        assert out["product"] == "viberpit"
        assert [(s["needsYou"], s["waiting"]) for s in out["samples"]] == [
            (4, {"blocked": 4}), (3, {"blocked": 3})]
        assert c.get("/api/pm/attention/history?product=nope",
                     headers=IDENTITY_HEADERS).status_code == 404
        assert c.get("/api/pm/attention/history?product=viberpit&days=0",
                     headers=IDENTITY_HEADERS).status_code == 422


def test_samples_are_the_map_badge_count_and_written_at_most_hourly():
    """The loop samples ``pm_tree.product_attention_counts`` — the map's own numbers —
    sets the gauge, and writes a row only once an hour."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import (AttentionSample, Feature, GhProject, GhProjectItem,
                                  Project, ProjectRepo, WorkItem)
    from server.obs.metrics import pm_needs_you
    from server.pm.tree import needs_you_total

    with _client() as c:
        async def _seed():
            async with db_session() as db:
                p = Project(slug="viberpit", name="viberpit")
                db.add(p)
                await db.flush()
                db.add(ProjectRepo(project_id=p.id, repo=REPO, is_primary=True))
                f = Feature(project_id=p.id, slug="cap", title="Cap", state="live")
                db.add(f)
                await db.flush()
                gp = GhProject(project_id=p.id, gh_project_number=1, title="b")
                db.add(gp)
                await db.flush()
                held = WorkItem(repo=REPO, number=1, kind="issue", state="open", title="h",
                                feature_id=f.id, hold_kind="decision", hold_by="tony",
                                hold_reason="pick one")
                waiting = WorkItem(repo=REPO, number=2, kind="issue", state="open",
                                   title="w", feature_id=f.id, labels=["parked"])
                db.add_all([held, waiting])
                await db.flush()
                for w, st in ((held, "Held"), (waiting, "Ready")):
                    db.add(GhProjectItem(gh_project_id=gp.id, work_item_id=w.id, status=st))
                await db.commit()
        _call(c, _seed)
        orch = c.app.state.sessions._orch
        assert _call(c, orch._needs_you_hygiene_samples) == ["viberpit"]
        assert _call(c, orch._needs_you_hygiene_samples) == []      # hourly, not per tick
        badge = needs_you_total(next(p for p in c.get(
            "/api/pm/map", headers=IDENTITY_HEADERS).json()["products"]
            if p["slug"] == "viberpit"))

        async def _rows():
            async with db_session() as db:
                return (await db.execute(select(AttentionSample))).scalars().all()
        rows = _call(c, _rows)
        assert [(r.needs_you, r.waiting["parked"]) for r in rows] == [(badge, 1)]
        assert badge >= 1
        assert pm_needs_you.value_for("viberpit") == badge


def test_load_reads_queue_rows_board_stamps_and_stale_edges():
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import GhProject, GhProjectItem, Project, WorkItem

    old = datetime.now(timezone.utc) - timedelta(days=3)
    with _client() as c:
        ids = _seed_stale_fixture(c)

        async def _board():
            async with db_session() as db:
                p = (await db.execute(select(Project))).scalar_one()
                gp = GhProject(project_id=p.id, gh_project_number=1, title="b")
                db.add(gp)
                await db.flush()
                db.add(GhProjectItem(gh_project_id=gp.id, work_item_id=ids[f"{REPO}#4"],
                                     status="Done", gh_updated_at=old))
                await db.commit()
                w4 = await db.get(WorkItem, ids[f"{REPO}#4"])
                return w4.id
        w4 = _call(c, _board)
        orch = c.app.state.sessions._orch
        [p] = _call(c, orch._needs_you_hygiene_load)
        assert p.slug == "viberpit" and [r.ref for r in p.rows] == [f"{REPO}#4"]
        assert abs((p.board_updated_at[w4] - old).total_seconds()) < 1
        assert {(e.src, e.blocker) for e in p.stale_edges if e.prunable} == {
            (f"{REPO}#1", f"{REPO}#10"), (f"{REPO}#3", f"{REPO}#4")}
