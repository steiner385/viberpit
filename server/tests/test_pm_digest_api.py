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
    """On top of ``test_pm_holds_api._seed`` (viberpit: #1 decision-held, #4 held by
    a person; kindash: #7 held): viberpit #6 held for a model approval, #8 with a
    high-confidence triage proposal and #9 with a medium one, a pending
    recommendation to move #4's card (it stands in for #4's hold), and a gate."""
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
                                 "hold_set_at": now}), (8, {}), (9, {})):
                wi = WorkItem(repo="o/r", number=n, kind="issue", title=f"issue {n}",
                              state="open", body="b", url=f"https://x/{n}", author="tony",
                              **hold)
                db.add(wi)
                await db.flush()
                db.add(GhProjectItem(gh_project_id=gh.id, work_item_id=wi.id, status="Backlog",
                                     score=None))
                ids[f"#{n}"] = wi.id
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
                                   status="awaiting_human", stage="spec")])
            await db.commit()
            return {**ids, "high": high.id, "medium": medium.id, "rec": rec.id}
    return _call(c, _run)


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
        # The recommendation stands in for #4's hold; the decision recommends its
        # proposal's recommended option (#3112).
        assert not [i for i in items.values() if i["ref"] == "o/r#4" and i["kind"] != "recommendation"]
        decision = [i for i in items.values() if i["ref"] == "o/r#1"]
        assert [(i["kind"], i["recommendation"]["action"], i["recommendation"]["label"])
                for i in decision] == [("decision", "accept_decision", "Accept: Move to B")]
        # A gate with no merged artifact carries no evidence: linked, not recommended.
        gate = items["gate:gate-1"]
        assert gate["recommendation"] is None and gate["url"] == "/initiatives/gate-1"
        assert body["total"] == len(items)
        assert body["withRecommendation"] == 4

        one = c.get("/api/pm/digest?product=kindash", headers=IDENTITY_HEADERS).json()
        assert [p["slug"] for p in one["products"]] == ["kindash"]
        assert c.get("/api/pm/digest?product=nope", headers=IDENTITY_HEADERS).status_code == 404


def test_apply_requires_confirm_and_dispatches_each_id_to_its_own_path():
    with _client() as c:
        _seed(c)
        ids = _seed_extras(c)
        wanted = [f"hold:{ids['#6']}", f"rec:{ids['rec']}", f"triage:{ids['high']}",
                  f"triage:{ids['medium']}", "gate:gate-1", "junk"]
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
            "gate:gate-1": (False, "no_recommendation"), "junk": (False, "bad_id")}
        assert (out["applied"], out["failed"]) == (3, 3)
        orch.backlog_runner_dispatch.assert_awaited_once_with(["o/r#6"], model="fable")
        assert [m[1] for m in moves] == [{"status": "Ready", "add_if_missing": False}]

        from sqlalchemy import select

        from server.db import db_session
        from server.db.models import AuditEvent, OwnerRecommendation, TriageProposal

        async def _read():
            async with db_session() as db:
                audits = {(a.type, a.identity) for a in (await db.execute(
                    select(AuditEvent))).scalars().all()}
                apply_rows = [a.detail for a in (await db.execute(select(AuditEvent).where(
                    AuditEvent.type == "pm.digest.apply"))).scalars().all()]
                rec = await db.get(OwnerRecommendation, ids["rec"])
                prop = await db.get(TriageProposal, ids["high"])
                return audits, apply_rows, rec.status, prop.status
        audits, apply_rows, rec_status, prop_status = _call(c, _read)
        assert rec_status == "applied" and prop_status == "accepted"
        # Each underlying action's own audit, plus ONE for the call.
        for t in ("pm.hold.escalation_approved", "pm.hygiene.board_blocked_no_blockers",
                  "pm.triage.accept"):
            assert (t, "testuser@example.com") in audits
        [row] = apply_rows
        assert row["ids"] == wanted and len(row["results"]) == 6

        # Applied items leave the digest.
        after = _by_id(c.get("/api/pm/digest", headers=IDENTITY_HEADERS).json())
        assert f"rec:{ids['rec']}" not in after and f"triage:{ids['high']}" not in after
        assert f"hold:{ids['#6']}" not in after


def _stub_announce(monkeypatch) -> list[tuple]:
    calls: list[tuple] = []

    def _fake(ref, *, label, detail, chosen_by):
        calls.append((ref, label, detail, chosen_by))
    monkeypatch.setattr("server.automations.backlog_github.announce_decision", _fake)
    return calls


def _apply(c, ids: list[str]) -> dict[str, tuple]:
    out = c.post("/api/pm/digest/apply", headers=IDENTITY_HEADERS,
                 json={"ids": ids, "confirm": True}).json()
    return {r["id"]: (r["ok"], r["step"]) for r in out["results"]}


def _decision_ids(c) -> tuple[str, str]:
    """(work item id of o/r#1, its pending proposal id)."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import DecisionProposal, WorkItem

    async def _run():
        async with db_session() as db:
            wi = (await db.execute(select(WorkItem).where(WorkItem.number == 1))).scalar_one()
            prop = (await db.execute(select(DecisionProposal).where(
                DecisionProposal.work_item_id == wi.id))).scalar_one()
            return wi.id, prop.id
    return _call(c, _run)


def test_apply_accepts_a_decision_through_the_accept_routes_sequence(monkeypatch):
    calls = _stub_announce(monkeypatch)
    with _client() as c:
        _seed(c)
        wid, pid = _decision_ids(c)
        assert _apply(c, [f"hold:{wid}"]) == {f"hold:{wid}": (True, None)}

        from sqlalchemy import select

        from server.db import db_session
        from server.db.models import AuditEvent, DecisionProposal, WorkItem

        async def _read():
            async with db_session() as db:
                prop = await db.get(DecisionProposal, pid)
                wi = await db.get(WorkItem, wid)
                audits = [(a.type, a.via, a.identity) for a in (await db.execute(
                    select(AuditEvent).where(AuditEvent.type.in_(
                        ("pm.decision.accept", "pm.hold.release"))))).scalars().all()]
                return prop.status, prop.chosen_key, prop.resolved_by, wi.hold_kind, audits
        status, chosen, by, hold, audits = _call(c, _read)
        assert (status, chosen, by, hold) == ("accepted", "b", "testuser@example.com", None)
        assert sorted(audits) == [("pm.decision.accept", "digest", "testuser@example.com"),
                                  ("pm.hold.release", "digest", "testuser@example.com")]
        # Said on the issue after the commit, through the accept route's own tail.
        assert calls == [("o/r#1", "Move to B", "b is better", "testuser@example.com")]
        # Already accepted: nothing to apply any more, by either id.
        assert _apply(c, [f"hold:{wid}", f"decision:{pid}"]) == {
            f"hold:{wid}": (False, "no_recommendation"),
            f"decision:{pid}": (False, "not_pending")}


def test_a_decision_whose_recommended_key_was_removed_is_refused(monkeypatch):
    _stub_announce(monkeypatch)
    with _client() as c:
        _seed(c)
        wid, pid = _decision_ids(c)
        from server.db import db_session
        from server.db.models import DecisionProposal

        async def _drop_b():
            async with db_session() as db:
                prop = await db.get(DecisionProposal, pid)
                prop.options = [o for o in prop.options if o["key"] != "b"] + [
                    {"key": "c", "label": "C", "detail": None}]
                await db.commit()
        _call(c, _drop_b)
        assert _apply(c, [f"hold:{wid}", f"decision:{pid}"]) == {
            f"hold:{wid}": (False, "no_recommendation"),
            f"decision:{pid}": (False, "no_recommendation")}

        async def _status():
            async with db_session() as db:
                return (await db.get(DecisionProposal, pid)).status
        assert _call(c, _status) == "pending"


def _seed_gates(c) -> None:
    """viberpit initiatives: ``spec-done`` awaiting the human at design with its spec
    merged (evidence), ``spec-open`` awaiting at design with the spec PR still open,
    ``built`` active at build with its epic fully closed (``advance``)."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import Initiative, InitiativeArtifact, Project, WorkItem

    async def _run():
        async with db_session() as db:
            p = (await db.execute(select(Project).where(Project.slug == "viberpit"))).scalar_one()
            done = Initiative(project_id=p.id, slug="spec-done", title="Spec done",
                              status="awaiting_human", stage="design")
            open_ = Initiative(project_id=p.id, slug="spec-open", title="Spec open",
                               status="awaiting_human", stage="design")
            built = Initiative(project_id=p.id, slug="built", title="Built", status="active",
                               stage="build", epic_repo="o/r", epic_number=50)
            db.add_all([done, open_, built])
            await db.flush()
            db.add_all([InitiativeArtifact(initiative_id=done.id, kind="spec", commit_sha="abc",
                                           pr_number=11),
                        InitiativeArtifact(initiative_id=open_.id, kind="spec", pr_number=12)])
            epic = WorkItem(repo="o/r", number=50, kind="issue", title="epic", state="open",
                            body="b", url="https://x/50", author="tony")
            db.add(epic)
            await db.flush()
            db.add_all([WorkItem(repo="o/r", number=n, kind="issue", title=f"sub {n}",
                                 state="closed", body="b", url=f"https://x/{n}", author="tony",
                                 parent_work_item_id=epic.id) for n in (51, 52)])
            await db.commit()
    _call(c, _run)


def test_a_gate_with_its_stage_evidence_recommends_and_applies_the_advance():
    with _client() as c:
        _seed(c)
        _seed_gates(c)
        items = _by_id(c.get("/api/pm/digest", headers=IDENTITY_HEADERS).json())
        assert items["gate:spec-done"]["recommendation"] == {
            "action": "advance_gate", "label": "Advance the stage gate",
            "params": {"slug": "spec-done"}}
        assert items["gate:built"]["recommendation"]["action"] == "advance_gate"
        assert items["gate:spec-open"]["recommendation"] is None

        assert _apply(c, ["gate:spec-done", "gate:built", "gate:spec-open", "gate:nope"]) == {
            "gate:spec-done": (True, None), "gate:built": (True, None),
            "gate:spec-open": (False, "no_recommendation"),
            "gate:nope": (False, "not_found")}

        from sqlalchemy import select

        from server.db import db_session
        from server.db.models import AuditEvent, Initiative

        async def _read():
            async with db_session() as db:
                stages = {i.slug: (i.stage, i.status) for i in (await db.execute(
                    select(Initiative))).scalars().all()}
                audits = [(a.detail["slug"], a.via) for a in (await db.execute(
                    select(AuditEvent).where(AuditEvent.type == "sdlc.initiative.advance")
                )).scalars().all()]
                return stages, audits
        stages, audits = _call(c, _read)
        assert stages["spec-done"] == ("plan", "active")
        assert stages["built"] == ("deploy", "active")
        assert stages["spec-open"] == ("design", "awaiting_human")
        assert sorted(audits) == [("built", "digest"), ("spec-done", "digest")]
        # Moved on: the gate no longer waits at the reason it was recommended for.
        assert _apply(c, ["gate:spec-done", "gate:built"]) == {
            "gate:spec-done": (False, "no_recommendation"),
            "gate:built": (False, "no_recommendation")}


def test_a_blocked_gate_is_refused_by_the_advance_routes_own_check():
    with _client() as c:
        _seed(c)
        _seed_gates(c)
        from sqlalchemy import select

        from server.db import db_session
        from server.db.models import Initiative

        async def _block():
            async with db_session() as db:
                ini = (await db.execute(select(Initiative).where(
                    Initiative.slug == "built"))).scalar_one()
                ini.status = "blocked"
                await db.commit()
        _call(c, _block)
        # ``blocked`` is neither awaiting_human nor the build "advance" reason.
        assert _apply(c, ["gate:built"]) == {"gate:built": (False, "no_recommendation")}


def _seed_noise_holds(c) -> dict[str, str]:
    """viberpit now skips ``forced-deploy-audit``: #12 and #14 carry a runner hold on
    such an issue, #13 a person's hold on one."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import GhProject, GhProjectItem, Project, WorkItem

    async def _run():
        async with db_session() as db:
            now = datetime.now(timezone.utc)
            p = (await db.execute(select(Project).where(Project.slug == "viberpit"))).scalar_one()
            p.automation_policy = {**(p.automation_policy or {}),
                                   "skip_labels": ["forced-deploy-audit"]}
            gh = (await db.execute(select(GhProject).where(
                GhProject.project_id == p.id))).scalar_one()
            ids = {}
            for n, by in ((12, "runner"), (13, "tony@example.com"), (14, "runner")):
                wi = WorkItem(repo="o/r", number=n, kind="issue", title=f"audit {n}",
                              state="open", body="b", url=f"https://x/{n}", author="tony",
                              labels=["forced-deploy-audit"], hold_kind="owner_action",
                              hold_by=by, hold_reason="owner call", hold_set_at=now)
                db.add(wi)
                await db.flush()
                db.add(GhProjectItem(gh_project_id=gh.id, work_item_id=wi.id, status="Ready",
                                     score=None))
                ids[f"#{n}"] = wi.id
            await db.commit()
            return ids
    return _call(c, _run)


def test_a_system_hold_the_policy_now_skips_recommends_and_applies_its_release():
    with _client() as c:
        _seed(c)
        ids = _seed_noise_holds(c)
        items = _by_id(c.get("/api/pm/digest", headers=IDENTITY_HEADERS).json())
        assert items[f"hold:{ids['#12']}"]["recommendation"] == {
            "action": "release_hold", "label": "Release the hold",
            "params": {"workItemId": ids["#12"]}}
        # A person's park on the same kind of issue is theirs.
        assert items[f"hold:{ids['#13']}"]["recommendation"] is None

        from server.db import db_session
        from server.db.models import AuditEvent, WorkItem

        async def _repark_12():
            async with db_session() as db:
                (await db.get(WorkItem, ids["#12"])).hold_by = "tony@example.com"
                await db.commit()
        _call(c, _repark_12)   # a person parks #12 between the read and the apply
        assert _apply(c, [f"hold:{ids['#12']}", f"hold:{ids['#13']}",
                          f"hold:{ids['#14']}"]) == {
            f"hold:{ids['#12']}": (False, "no_recommendation"),
            f"hold:{ids['#13']}": (False, "no_recommendation"),
            f"hold:{ids['#14']}": (True, None)}

        from sqlalchemy import select

        async def _read():
            async with db_session() as db:
                holds = {n: (await db.get(WorkItem, ids[n])).hold_by for n in ids}
                rel = [(a.detail.get("signal"), a.via, a.identity) for a in (await db.execute(
                    select(AuditEvent).where(AuditEvent.type == "pm.hold.release")
                )).scalars().all()]
                return holds, rel
        holds, rel = _call(c, _read)
        assert holds == {"#12": "tony@example.com", "#13": "tony@example.com", "#14": None}
        assert rel == [("policy_skip", "digest", "testuser@example.com")]
