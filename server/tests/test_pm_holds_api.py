"""GET /api/pm/holds — the escalation read model on the wire (spec 2026-09-30 §4.2)."""
from __future__ import annotations

from datetime import datetime, timezone

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
                p = Project(slug=slug, name=slug, automation_policy={"dispatch": "auto"})
                db.add(p)
                await db.flush()
                db.add(ProjectRepo(project_id=p.id, repo=repo, is_primary=True))
                gh = GhProject(project_id=p.id, gh_project_number=number, title=slug)
                db.add(gh)
                await db.flush()
                return gh

            async def item(gh, repo, n, **hold):
                wi = WorkItem(repo=repo, number=n, kind="issue", title=f"issue {n}",
                              state="open", body="b", url=f"https://x/{n}", **hold)
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
        assert by_slug["viberpit"] == {"slug": "viberpit", "dispatchable": 1,
                                       "skipped": {"held": 2, "blocked": 2},
                                       "heldCount": 2, "starving": True,
                                       "oldestHoldHours": by_slug["viberpit"]["oldestHoldHours"]}
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
