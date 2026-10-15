"""GET /api/pm/digest + POST /api/pm/digest/apply (design 2026-10-14 §5): the owner
digest on the wire, and each recommended action dispatched to the path that owns it."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from server.tests.pm_ownership_fixtures import identity_repositories
from server.tests.test_pm_holds_api import _seed
from server.tests.test_pm_queue_api import IDENTITY_HEADERS, _client


@pytest.fixture(autouse=True)
def canonical_mutation_repositories(monkeypatch):
    """Repo identity resolution without GitHub (a triage accept's ownership preflight)."""
    monkeypatch.setattr("server.automations.target_identity.resolve_repositories",
                        identity_repositories)


def _call(c, fn):
    assert c.portal is not None
    return c.portal.call(fn)


def _seed_extras(c) -> dict[str, str]:
    """On top of ``test_pm_holds_api._seed`` (viberpit: #1 decision-held with ``b``
    recommended, #4 held by a person; kindash: #7 held): viberpit #6 held for a model
    approval, #8 with a high-confidence triage proposal and #9 with a medium one, #10
    a self-closing tracker the runner handed off anyway (#3112: release), a pending
    recommendation to move #4's card (it stands in for #4's hold), a gate awaiting
    review, and a gate whose build epic (#20, one closed sub-issue #21) is done
    (#3112: advance)."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import (Feature, GhProject, GhProjectItem, Initiative,
                                  OwnerRecommendation, Project, TriageProposal, WorkItem)

    async def _run() -> dict[str, str]:
        async with db_session() as db:
            now = datetime.now(timezone.utc)
            p = (await db.execute(select(Project).where(Project.slug == "viberpit"))).scalar_one()
            gh = (await db.execute(select(GhProject).where(
                GhProject.project_id == p.id))).scalar_one()
            feat = Feature(project_id=p.id, slug="sessions", title="Sessions", state="live")
            db.add(feat)
            ids: dict[str, str] = {}
            for n, hold in ((6, {"hold_kind": "owner_action", "hold_by": "runner",
                                 "hold_reason": "bounced at opus; fable needs approval",
                                 "hold_set_at": now}), (8, {}), (9, {}),
                            (10, {"hold_kind": "owner_action", "hold_by": "runner",
                                  "hold_reason": "bounced at opus; fable needs approval",
                                  "hold_set_at": now, "labels": ["red-watchdog"]})):
                wi = WorkItem(repo="o/r", number=n, kind="issue", title=f"issue {n}",
                              state="open", body="b", url=f"https://x/{n}", author="tony",
                              **hold)
                db.add(wi)
                await db.flush()
                db.add(GhProjectItem(gh_project_id=gh.id, work_item_id=wi.id, status="Backlog",
                                     score=None))
                ids[f"#{n}"] = wi.id
            epic = WorkItem(repo="o/r", number=20, kind="issue", title="epic", state="open",
                            body="", url="https://x/20", author="tony", is_epic=True)
            db.add(epic)
            await db.flush()
            db.add(WorkItem(repo="o/r", number=21, kind="issue", title="child", state="closed",
                            body="", url="https://x/21", author="tony",
                            parent_work_item_id=epic.id))
            await db.flush()
            high = TriageProposal(work_item_id=ids["#8"], proposed_feature_id=feat.id,
                                  confidence_level="high", reach=1, impact=1, effort=1,
                                  confidence=80, rationale="fits", status="pending")
            medium = TriageProposal(work_item_id=ids["#9"], proposed_feature_id=feat.id,
                                    confidence_level="medium", reach=1, impact=1, effort=1,
                                    confidence=50, rationale="maybe", status="pending")
            w4 = (await db.execute(select(WorkItem).where(WorkItem.number == 4))).scalar_one()
            rec = OwnerRecommendation(project_id=p.id, rule="board_blocked_no_blockers",
                                      target_ref="o/r#4", work_item_id=w4.id,
                                      action="set_status",
                                      detail={"status": "Ready", "title": "issue 4"},
                                      evidence="o/r#4 sits in Blocked but nothing blocks it")
            db.add_all([high, medium, rec,
                        Initiative(project_id=p.id, slug="gate-1", title="Gate one",
                                   status="awaiting_human", stage="spec"),
                        Initiative(project_id=p.id, slug="gate-2", title="Gate two",
                                   status="active", stage="build", epic_repo="o/r",
                                   epic_number=20)])
            await db.commit()
            return {**ids, "high": high.id, "medium": medium.id, "rec": rec.id}
    return _call(c, _run)


@pytest.fixture
def no_issue_comment(monkeypatch):
    """An accepted decision's post-commit issue comment, recorded instead of shelled
    out (``backlog_github.announce_decision`` runs ``gh``)."""
    posted: list[tuple] = []
    monkeypatch.setattr("server.automations.backlog_github.announce_decision",
                        lambda ref, **kw: posted.append((ref, kw)))
    return posted


def _by_id(body: dict) -> dict[str, dict]:
    return {i["id"]: i for p in body["products"] for i in p["items"]}


def test_digest_lists_what_needs_you_with_one_recommendation_each():
    with _client() as c:
        _seed(c)
        ids = _seed_extras(c)
        body = c.get("/api/pm/digest", headers=IDENTITY_HEADERS).json()
        items = _by_id(body)
        assert [p["slug"] for p in body["products"]] == ["kindash", "viberpit"]
        approval = items[f"hold:{ids['#6']}"]
        assert approval["kind"] == "model_approval"
        assert approval["recommendation"]["params"]["model"] == "fable"
        assert items[f"triage:{ids['high']}"]["recommendation"]["action"] == "accept_triage"
        assert items[f"triage:{ids['medium']}"]["recommendation"] is None
        rec = items[f"rec:{ids['rec']}"]
        assert rec["recommendation"]["label"] == "Move to Ready" and rec["ref"] == "o/r#4"
        # The recommendation stands in for #4's hold; the decision hold recommends
        # the option its proposer recommended (#3112).
        assert not [i for i in items.values() if i["ref"] == "o/r#4" and i["kind"] != "recommendation"]
        [decision] = [i for i in items.values() if i["ref"] == "o/r#1"]
        assert decision["kind"] == "decision"
        assert decision["recommendation"]["action"] == "accept_decision"
        assert decision["recommendation"]["label"] == "Accept Move to B"
        assert decision["recommendation"]["params"]["chosenKey"] == "b"
        # #10 is a self-closing tracker the runner handed off anyway: release, not approve.
        stale = items[f"hold:{ids['#10']}"]
        assert stale["recommendation"]["action"] == "release_hold"
        assert "skips this issue anyway" in stale["why"]
        gate = items["gate:gate-1"]
        assert gate["recommendation"] is None and gate["url"] == "/initiatives/gate-1"
        ready = items["gate:gate-2"]
        assert ready["recommendation"] == {"action": "advance_initiative",
                                           "label": "Advance to deploy",
                                           "params": {"slug": "gate-2", "toStage": "deploy"}}
        assert body["total"] == len(items)
        assert body["withRecommendation"] == 6

        one = c.get("/api/pm/digest?product=kindash", headers=IDENTITY_HEADERS).json()
        assert [p["slug"] for p in one["products"]] == ["kindash"]
        assert c.get("/api/pm/digest?product=nope", headers=IDENTITY_HEADERS).status_code == 404


def test_apply_requires_confirm_and_dispatches_each_id_to_its_own_path(no_issue_comment):
    with _client() as c:
        _seed(c)
        ids = _seed_extras(c)
        from sqlalchemy import select as _select

        from server.db import db_session as _db_session
        from server.db.models import WorkItem as _WorkItem

        async def _w1():
            async with _db_session() as db:
                return (await db.execute(_select(_WorkItem.id).where(
                    _WorkItem.repo == "o/r", _WorkItem.number == 1))).scalar_one()
        w1 = _call(c, _w1)
        wanted = [f"hold:{ids['#6']}", f"rec:{ids['rec']}", f"triage:{ids['high']}",
                  f"triage:{ids['medium']}", "gate:gate-1", "junk",
                  f"hold:{w1}", f"hold:{ids['#10']}", "gate:gate-2"]
        assert c.post("/api/pm/digest/apply", headers=IDENTITY_HEADERS,
                      json={"ids": wanted}).status_code == 422

        orch = c.app.state.sessions
        orch.backlog_runner_dispatch = AsyncMock(return_value={
            "status": "spawned", "session_id": "s-9"})
        orch.resolve_model_approval = AsyncMock(return_value=None)
        moves: list[tuple] = []

        async def board_status(work_item_id, request):
            moves.append((work_item_id, request))
            return {"ok": True}
        orch.pm_board_status = board_status
        out = c.post("/api/pm/digest/apply", headers=IDENTITY_HEADERS,
                     json={"ids": wanted, "confirm": True}).json()
        steps = {r["id"]: (r["ok"], r["step"]) for r in out["results"]}
        assert steps == {
            f"hold:{ids['#6']}": (True, None), f"rec:{ids['rec']}": (True, None),
            f"triage:{ids['high']}": (True, None),
            f"triage:{ids['medium']}": (False, "no_recommendation"),
            "gate:gate-1": (False, "no_recommendation"), "junk": (False, "bad_id"),
            f"hold:{w1}": (True, None), f"hold:{ids['#10']}": (True, None),
            "gate:gate-2": (True, None)}
        assert (out["applied"], out["failed"]) == (6, 3)
        orch.backlog_runner_dispatch.assert_awaited_once_with(["o/r#6"], model="fable")
        assert [m[1] for m in moves] == [{"status": "Ready", "add_if_missing": False}]
        # The accepted decision was announced on its issue, after the commit.
        assert [(ref, kw["label"]) for ref, kw in no_issue_comment] == [("o/r#1", "Move to B")]

        from sqlalchemy import select

        from server.db import db_session
        from server.db.models import (AuditEvent, DecisionProposal, Initiative,
                                      OwnerRecommendation, TriageProposal, WorkItem)

        async def _read():
            async with db_session() as db:
                audits = {(a.type, a.identity) for a in (await db.execute(
                    select(AuditEvent))).scalars().all()}
                apply_rows = [a.detail for a in (await db.execute(select(AuditEvent).where(
                    AuditEvent.type == "pm.digest.apply"))).scalars().all()]
                rec = await db.get(OwnerRecommendation, ids["rec"])
                prop = await db.get(TriageProposal, ids["high"])
                decision = (await db.execute(select(DecisionProposal).where(
                    DecisionProposal.work_item_id == w1))).scalar_one()
                w1_row = await db.get(WorkItem, w1)
                w10 = await db.get(WorkItem, ids["#10"])
                gate2 = (await db.execute(select(Initiative).where(
                    Initiative.slug == "gate-2"))).scalar_one()
                return (audits, apply_rows, rec.status, prop.status,
                        (decision.status, decision.chosen_key, w1_row.hold_kind),
                        w10.hold_kind, (gate2.stage, gate2.status))
        (audits, apply_rows, rec_status, prop_status, decided, w10_hold,
         gate2) = _call(c, _read)
        assert rec_status == "applied" and prop_status == "accepted"
        assert decided == ("accepted", "b", None)
        assert w10_hold is None
        assert gate2 == ("deploy", "active")
        # Each underlying action's own audit, plus ONE for the call.
        for t in ("pm.hold.escalation_approved", "pm.hygiene.board_blocked_no_blockers",
                  "pm.triage.accept", "pm.decision.accept", "pm.hold.release",
                  "sdlc.initiative.advance"):
            assert (t, "testuser@example.com") in audits
        [row] = apply_rows
        assert row["ids"] == wanted and len(row["results"]) == 9

        # Applied items leave the digest.
        after = _by_id(c.get("/api/pm/digest", headers=IDENTITY_HEADERS).json())
        assert f"rec:{ids['rec']}" not in after and f"triage:{ids['high']}" not in after
        assert f"hold:{ids['#6']}" not in after
        assert f"hold:{w1}" not in after and f"hold:{ids['#10']}" not in after
        assert "gate:gate-2" not in after
