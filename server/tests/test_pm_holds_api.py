"""GET /api/pm/holds — the escalation read model on the wire (spec 2026-09-30 §4.2)."""
from __future__ import annotations

from datetime import datetime, timezone

from server.tests._trust_ids import TONY_ID, TONY_ENTRY
from server.tests.test_pm_queue_api import IDENTITY_HEADERS, _client

OPTIONS = [{"key": "a", "label": "Keep A", "detail": None},
           {"key": "b", "label": "Move to B", "detail": "b is better"}]


def _seed(c) -> None:
    """Two products. viberpit: #1 decision-held with a pending proposal, #2 and #3
    behind it (#3 behind #2), #4 owner_action-held with nothing behind it, #5 free.
    kindash: #7 owner_action-held, nothing else — starving."""
    from server.db import db_session
    from server.db.models import (GhProject, GhProjectItem, Project, ProjectRepo, WorkItem,
                                  WorkItemDep)
    from server.pm import decisions

    async def _run() -> None:
        async with db_session() as db:
            now = datetime.now(timezone.utc)

            async def product(slug, repo, number):
                p = Project(slug=slug, name=slug, automation_policy={"dispatch": "auto",
                                                       "trusted_authors": [TONY_ENTRY]})
                db.add(p)
                await db.flush()
                db.add(ProjectRepo(project_id=p.id, repo=repo, is_primary=True))
                gh = GhProject(project_id=p.id, gh_project_number=number, title=slug)
                db.add(gh)
                await db.flush()
                return gh

            async def item(gh, repo, n, **hold):
                wi = WorkItem(repo=repo, number=n, kind="issue", title=f"issue {n}",
                              state="open", body="b", url=f"https://x/{n}",
                              author="tony", author_id=TONY_ID, **hold)
                db.add(wi)
                await db.flush()
                db.add(GhProjectItem(gh_project_id=gh.id, work_item_id=wi.id,
                                     status="Ready", score=float(n)))
                return wi

            gh_v = await product("viberpit", "o/r", 1)
            w1 = await item(gh_v, "o/r", 1)
            await item(gh_v, "o/r", 2)
            await item(gh_v, "o/r", 3)
            await item(gh_v, "o/r", 4, hold_kind="owner_action", hold_by="tony@example.com",
                       hold_reason="needs the keyring", hold_set_at=now)
            await item(gh_v, "o/r", 5)
            db.add(WorkItemDep(src_ref="o/r#2", dst_ref="o/r#1", source="native", active=True))
            db.add(WorkItemDep(src_ref="o/r#3", dst_ref="o/r#2", source="native", active=True))
            await decisions.propose(db, w1, options=OPTIONS, recommended_key="b",
                                    rationale="two writers", actor="session:s1", now=now)
            gh_k = await product("kindash", "o/k", 2)
            await item(gh_k, "o/k", 7, hold_kind="owner_action", hold_by="tony@example.com",
                       hold_reason="owner call", hold_set_at=now)
            await db.commit()

    assert c.portal is not None
    c.portal.call(_run)


def test_holds_lists_parked_items_ranked_by_impact_with_product_summaries():
    with _client() as c:
        _seed(c)
        body = c.get("/api/pm/holds", headers=IDENTITY_HEADERS).json()

        assert [i["ref"] for i in body["held"]] == ["o/r#1", "o/k#7", "o/r#4"]
        top = body["held"][0]
        assert top["holdKind"] == "decision" and top["product"] == "viberpit"
        assert top["proposalId"] and top["recommendedKey"] == "b"
        assert [o["key"] for o in top["options"]] == ["a", "b"]
        assert top["downstream"] == 2 and top["downstreamDispatchable"] == 2
        assert top["downstreamRefs"] == ["o/r#2", "o/r#3"]
        assert top["releasedBy"] == "Accept an option in the Inbox"
        assert top["holdBy"] == "session:s1" and top["holdSetAt"] and top["ageHours"] >= 0
        assert body["held"][2]["proposalId"] is None
        assert body["held"][2]["releasedBy"].startswith("Retry")

        by_slug = {p["slug"]: p for p in body["products"]}
        oldest = by_slug["viberpit"].pop("oldestHoldHours")
        assert isinstance(oldest, (int, float)) and oldest >= 0
        assert by_slug["viberpit"] == {"slug": "viberpit", "dispatchable": 1,
                                       "skipped": {"held": 2, "blocked": 2},
                                       "heldCount": 2, "starving": True}
        assert by_slug["kindash"]["starving"] is True and by_slug["kindash"]["dispatchable"] == 0
        assert body["truncated"] == 0 and body["generatedAt"]


def test_holds_filters_by_product_and_kind_and_limits():
    with _client() as c:
        _seed(c)
        body = c.get("/api/pm/holds?product=viberpit&kind=owner_action",
                     headers=IDENTITY_HEADERS).json()
        assert [i["ref"] for i in body["held"]] == ["o/r#4"]
        assert [p["slug"] for p in body["products"]] == ["viberpit"]

        body = c.get("/api/pm/holds?limit=1", headers=IDENTITY_HEADERS).json()
        assert [i["ref"] for i in body["held"]] == ["o/r#1"] and body["truncated"] == 2

        assert c.get("/api/pm/holds?kind=evidence", headers=IDENTITY_HEADERS).status_code == 422
        assert c.get("/api/pm/holds?min_status=Done", headers=IDENTITY_HEADERS).status_code == 422
        # An unknown slug is an empty answer, not an error.
        empty = c.get("/api/pm/holds?product=nope", headers=IDENTITY_HEADERS).json()
        assert empty["products"] == [] and empty["held"] == [] and empty["truncated"] == 0


def test_holds_lists_a_decision_hold_whose_proposal_row_is_gone():
    with _client() as c:
        _seed(c)
        from sqlalchemy import delete

        from server.db import db_session
        from server.db.models import DecisionProposal

        async def _drop():
            async with db_session() as db:
                await db.execute(delete(DecisionProposal))
                await db.commit()
        c.portal.call(_drop)
        body = c.get("/api/pm/holds?product=viberpit&kind=decision",
                     headers=IDENTITY_HEADERS).json()
        (row,) = body["held"]
        assert row["proposalId"] is None and row["options"] == []


def test_decisions_impact_reads_the_same_model_as_holds():
    with _client() as c:
        _seed(c)
        (row,) = c.get("/api/pm/decisions?impact=true", headers=IDENTITY_HEADERS).json()
        assert row["ref"] == "o/r#1" and row["downstream"] == 2
        assert row["downstreamDispatchable"] == 2


def _seed_backlog_reach(c) -> None:
    """A product whose policy reaches down to Backlog: #10 owner_action-held (Ready),
    #11 behind it with board Status Backlog."""
    from server.db import db_session
    from server.db.models import (GhProject, GhProjectItem, Project, ProjectRepo, WorkItem,
                                  WorkItemDep)

    async def _run() -> None:
        async with db_session() as db:
            now = datetime.now(timezone.utc)
            p = Project(slug="deep", name="deep",
                        automation_policy={"dispatch": "auto", "queue_min_status": "Backlog",
                                           "trusted_authors": [TONY_ENTRY]})
            db.add(p)
            await db.flush()
            db.add(ProjectRepo(project_id=p.id, repo="o/d", is_primary=True))
            gh = GhProject(project_id=p.id, gh_project_number=9, title="deep")
            db.add(gh)
            await db.flush()
            for n, status, hold in ((10, "Ready", {"hold_kind": "owner_action",
                                                   "hold_by": "tony@example.com",
                                                   "hold_reason": "call", "hold_set_at": now}),
                                    (11, "Backlog", {})):
                wi = WorkItem(repo="o/d", number=n, kind="issue", title=f"issue {n}",
                              state="open", body="b", url=f"https://x/{n}",
                              author="tony", author_id=TONY_ID, **hold)
                db.add(wi)
                await db.flush()
                db.add(GhProjectItem(gh_project_id=gh.id, work_item_id=wi.id,
                                     status=status, score=float(n)))
            db.add(WorkItemDep(src_ref="o/d#11", dst_ref="o/d#10", source="native", active=True))
            await db.commit()

    assert c.portal is not None
    c.portal.call(_run)


def test_holds_and_decisions_impact_share_the_products_reach():
    from server.db import db_session
    from server.db.models import WorkItem
    from server.pm import decisions
    from sqlalchemy import select

    with _client() as c:
        _seed_backlog_reach(c)

        async def _propose():
            async with db_session() as db:
                w = (await db.execute(select(WorkItem).where(WorkItem.number == 10))).scalar_one()
                # A decision hold replaces the owner_action one so the list carries it.
                w.hold_kind = None
                await db.flush()
                await decisions.propose(db, w, options=OPTIONS, recommended_key="b",
                                        rationale="r", actor="session:s1",
                                        now=datetime.now(timezone.utc))
                await db.commit()
        c.portal.call(_propose)

        held = c.get("/api/pm/holds?product=deep", headers=IDENTITY_HEADERS).json()["held"]
        (h,) = [i for i in held if i["ref"] == "o/d#10"]
        (d,) = [r for r in c.get("/api/pm/decisions?impact=true&projectSlug=deep",
                                 headers=IDENTITY_HEADERS).json() if r["ref"] == "o/d#10"]
        assert (h["downstream"], h["downstreamDispatchable"]) == (1, 1)
        assert (d["downstream"], d["downstreamDispatchable"]) == (1, 1)

        what_if = c.get("/api/pm/holds?product=deep&min_status=Ready",
                        headers=IDENTITY_HEADERS).json()["held"]
        (w,) = [i for i in what_if if i["ref"] == "o/d#10"]
        assert w["downstream"] == 1 and w["downstreamDispatchable"] == 0


def test_holds_names_the_row_and_the_approvable_escalation():
    """A hold the runner wrote at an approval-clamped ladder top ("bounced at opus;
    fable needs approval") is the one the Roadmap offers "Approve escalation to
    fable" for — so the wire carries the work-item id to act on and the parsed
    escalation, and ``releasedBy`` names that fourth way out. An ordinary
    owner_action hold carries ``escalation: null``."""
    from server.db import db_session
    from server.db.models import WorkItem
    from sqlalchemy import select, update

    with _client() as c:
        _seed(c)

        async def _escalate():
            async with db_session() as db:
                await db.execute(update(WorkItem).where(WorkItem.repo == "o/k", WorkItem.number == 7)
                                 .values(hold_reason="bounced at opus; fable needs approval",
                                         hold_by="runner"))
                await db.commit()
                return (await db.execute(select(WorkItem.id).where(
                    WorkItem.repo == "o/k", WorkItem.number == 7))).scalar_one()
        wid = c.portal.call(_escalate)
        body = c.get("/api/pm/holds?kind=owner_action", headers=IDENTITY_HEADERS).json()
        by_ref = {i["ref"]: i for i in body["held"]}
        gated = by_ref["o/k#7"]
        assert gated["workItemId"] == wid
        assert gated["escalation"] == {"bouncedAt": "opus", "needsApproval": "fable"}
        assert gated["releasedBy"].endswith("or approving the escalation to fable")
        plain = by_ref["o/r#4"]
        assert plain["workItemId"] and plain["escalation"] is None
        assert plain["releasedBy"] == "Retry, a board move to Ready/Backlog, or an issue-body edit"
        assert (gated["attentionKind"], plain["attentionKind"]) == ("model_approval",
                                                                   "owner_action")
