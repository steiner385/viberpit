"""Delegated roadmap tools use the existing PM writers and their conflicts."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from server.db import db_session
from server.db.models import AuditEvent, WorkItem
from server.sessions.tools.context import SessionToolContext
from server.sessions.tools.tools import build_session_tools


def context(identity="owner@example.com", mode="default"):
    return SessionToolContext(session_id="triage-session", identity=identity, kind=None,
                              engine="codex", signalling_tools=True,
                              record=lambda: SimpleNamespace(permission_mode=mode))


async def call(name, args, ctx=None):
    tool = next(t for t in build_session_tools(ctx or context()) if t.name == name)
    return await tool.handler(args)


@pytest.mark.asyncio
async def test_decision_resolution_audits_delegation_and_rejects_stale(isolated_db, monkeypatch):
    from server.pm import decisions
    async def announce(*a, **kw):
        pass
    monkeypatch.setattr(decisions, "announce_accept_best_effort", announce)
    async with db_session() as db:
        wi = WorkItem(repo="o/r", number=1, kind="issue", body="Pick one")
        db.add(wi)
        await db.flush()
        prop = await decisions.propose(db, wi, options=[{"key": "keep", "label": "Keep"},
                                                       {"key": "change", "label": "Change"}],
                                       recommended_key="keep", rationale="Less churn",
                                       actor="session:author", now=datetime.now(timezone.utc))
        proposal_id = prop.id
    args = {"kind": "decision", "proposal_id": proposal_id, "action": "accept", "chosen_key": "keep"}
    out = await call("roadmap_resolve", args)
    assert not out.get("is_error"), out
    assert json.loads(out["content"][0]["text"])["data"]["status"] == "accepted"
    stale = await call("roadmap_resolve", args)
    assert stale.get("is_error") and "409" in stale["content"][0]["text"]
    async with db_session() as db:
        audits = (await db.execute(select(AuditEvent).where(
            AuditEvent.type == "pm.session.delegate"))).scalars().all()
        assert len(audits) == 1
        assert audits[0].identity == "owner@example.com" and audits[0].via == "session"
        assert audits[0].detail["sessionId"] == "triage-session"


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["", "lan@local", "automation:runner", "session:other", "assistant:owner"])
async def test_delegation_requires_person(isolated_db, identity):
    out = await call("roadmap_hold", {"item_id": "missing", "reason": "wait", "action": "hold"}, context(identity))
    assert out.get("is_error") and "human owner" in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_owner_hold_and_release(isolated_db):
    async with db_session() as db:
        wi = WorkItem(repo="o/r", number=1, kind="issue", body="work")
        db.add(wi)
        await db.flush()
        item_id = wi.id
    out = await call("roadmap_hold", {"item_id": item_id, "action": "hold", "reason": "Wait for my review"})
    assert not out.get("is_error"), out
    async with db_session() as db:
        wi = await db.get(WorkItem, item_id)
        assert wi.hold_by == "owner@example.com" and wi.hold_kind == "owner_action"
    out = await call("roadmap_hold", {"item_id": item_id, "action": "release"})
    assert not out.get("is_error"), out


@pytest.mark.asyncio
async def test_invalid_or_impersonating_arguments_are_refused(isolated_db):
    out = await call("roadmap_resolve", {"kind": "decision", "proposal_id": "id", "action": "accept", "identity": "other"})
    assert out.get("is_error")


@pytest.mark.asyncio
async def test_inventory_reads_real_map_with_pagination_and_product_scope(isolated_db):
    from server.db.models import Feature, Initiative, Project, ProjectRepo, TriageProposal
    async with db_session() as db:
        project = Project(slug="viberpit", name="ViberPit")
        other = Project(slug="elsewhere", name="Elsewhere")
        db.add_all([project, other])
        await db.flush()
        cap = Feature(project_id=project.id, slug="sessions", title="Sessions", state="live")
        db.add_all([cap, ProjectRepo(project_id=project.id, repo="o/r"),
                    Initiative(project_id=project.id, slug="e2", title="Accessible", stage="design", status="awaiting_human"),
                    Initiative(project_id=other.id, slug="other", stage="design", status="awaiting_human")])
        await db.flush()
        wi = WorkItem(repo="o/r", number=1, kind="issue", title="Held", body="Context", state="open",
                      feature_id=cap.id, hold_kind="owner_action", hold_reason="review", triage_state="classified")
        homeless = WorkItem(repo="o/r", number=2, kind="issue", title="Needs home", state="open", hold_kind="owner_action")
        db.add_all([wi, homeless])
        await db.flush()
        db.add(TriageProposal(work_item_id=wi.id, proposed_feature_id=cap.id, rationale="Fits sessions"))
        wid = wi.id
    out = await call("roadmap_needs_you", {"product": "viberpit", "limit": 2})
    assert not out.get("is_error"), out
    data = json.loads(out["content"][0]["text"])["data"]
    assert data["total"] == 4 and data["next_offset"] == 2
    assert [i["kind"] for i in data["items"]] == ["classification", "initiative"]
    out = await call("roadmap_needs_you", {"product": "viberpit", "offset": 2})
    data = json.loads(out["content"][0]["text"])["data"]
    assert len(data["items"]) == 2 and data["next_offset"] is None
    detail = await call("roadmap_item", {"kind": "work_item", "id": wid})
    assert json.loads(detail["content"][0]["text"])["data"]["body"] == "Context"


@pytest.mark.asyncio
async def test_advance_preserves_block_and_stale_stage_guards(isolated_db):
    from server.db.models import Initiative
    async with db_session() as db:
        db.add_all([Initiative(slug="ready", stage="design", status="awaiting_human"),
                    Initiative(slug="blocked", stage="design", status="blocked")])
    args = {"slug": "ready", "expected_stage": "design"}
    out = await call("roadmap_advance", args)
    assert not out.get("is_error"), out
    out = await call("roadmap_advance", args)
    assert out.get("is_error") and "stale_stage" in out["content"][0]["text"]
    out = await call("roadmap_advance", {"slug": "blocked", "expected_stage": "design"})
    assert out.get("is_error") and "blocked" in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_classification_reject_uses_canonical_hold_and_conflict(isolated_db):
    from server.db.models import TriageProposal
    async with db_session() as db:
        wi = WorkItem(repo="o/r", number=1, kind="issue", state="open")
        db.add(wi)
        await db.flush()
        prop = TriageProposal(work_item_id=wi.id)
        db.add(prop)
        await db.flush()
        pid, wid = prop.id, wi.id
    args = {"kind": "classification", "proposal_id": pid, "action": "reject"}
    out = await call("roadmap_resolve", args)
    assert not out.get("is_error"), out
    out = await call("roadmap_resolve", args)
    assert out.get("is_error") and "409" in out["content"][0]["text"]
    async with db_session() as db:
        wi = await db.get(WorkItem, wid)
        assert wi.hold_kind == "owner_action" and wi.hold_by == "owner@example.com"


@pytest.mark.asyncio
async def test_native_bypass_mode_cannot_delegate(isolated_db):
    out = await call("roadmap_hold", {"item_id": "missing", "action": "release"},
                     context(mode="bypassPermissions"))
    assert out.get("is_error") and "bypassPermissions" in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_decision_hold_cannot_be_released(isolated_db):
    async with db_session() as db:
        wi = WorkItem(repo="o/r", number=1, kind="issue", hold_kind="decision")
        db.add(wi)
        await db.flush()
        wid = wi.id
    out = await call("roadmap_hold", {"item_id": wid, "action": "release"})
    assert out.get("is_error") and "proposal" in out["content"][0]["text"]
    async with db_session() as db:
        assert (await db.get(WorkItem, wid)).hold_kind == "decision"


@pytest.mark.asyncio
@pytest.mark.parametrize("verified", [False, True])
async def test_classification_accept_preserves_repository_ownership_guard(isolated_db, monkeypatch, verified):
    if verified:
        from server.tests.pm_ownership_fixtures import identity_repositories
        monkeypatch.setattr("server.automations.target_identity.resolve_repositories", identity_repositories)
    from server.db.models import Feature, Project, ProjectRepo, TriageProposal
    async with db_session() as db:
        project = Project(slug="sample")
        db.add(project)
        await db.flush()
        feature = Feature(project_id=project.id, slug="sessions", state="live")
        wi = WorkItem(repo="o/r", number=1, kind="issue", state="open")
        db.add_all([feature, wi, ProjectRepo(project_id=project.id, repo="o/r")])
        await db.flush()
        prop = TriageProposal(work_item_id=wi.id, proposed_feature_id=feature.id,
                              reach=3, impact=4, effort=2, confidence=80)
        db.add(prop)
        await db.flush()
        pid, wid, fid = prop.id, wi.id, feature.id
    out = await call("roadmap_resolve", {"kind": "classification", "proposal_id": pid, "action": "accept"})
    if verified:
        assert not out.get("is_error"), out
    else:
        assert out.get("is_error") and "ownership" in out["content"][0]["text"]
    async with db_session() as db:
        wi, prop = await db.get(WorkItem, wid), await db.get(TriageProposal, pid)
        if verified:
            assert wi.feature_id == fid and wi.reach == 3 and wi.confidence == 80
            assert prop.status == "accepted" and prop.undo_snapshot is not None
            audits = (await db.execute(select(AuditEvent).where(
                AuditEvent.type == "pm.session.delegate"))).scalars().all()
            assert len(audits) == 1 and audits[0].identity == "owner@example.com"
        else:
            assert wi.feature_id is None
            assert prop.status == "pending" and prop.undo_snapshot is None


@pytest.mark.asyncio
async def test_inventory_includes_held_pr_when_capability_issue_counts_are_zero(isolated_db):
    from server.api.pm_tree import get_map
    from server.db.models import Feature, Project
    async with db_session() as db:
        project = Project(slug="sample", name="Sample")
        db.add(project)
        await db.flush()
        feature = Feature(project_id=project.id, slug="sessions", title="Sessions", state="live")
        db.add(feature)
        await db.flush()
        pr = WorkItem(repo="o/r", number=3, kind="pr", title="Review this change", state="open",
                      feature_id=feature.id, hold_kind="owner_action", hold_reason="Needs review")
        db.add(pr)
        await db.flush()
        pr_id = pr.id
        roadmap = await get_map(_identity="owner@example.com", db=db)
        capability = roadmap["products"][0]["capabilities"][0]
        assert capability["counts"]["needsYou"] == 0
    out = await call("roadmap_needs_you", {"product": "sample"})
    assert not out.get("is_error"), out
    data = json.loads(out["content"][0]["text"])["data"]
    assert data["total"] == 1
    assert data["items"][0]["id"] == pr_id
    assert data["items"][0]["itemKind"] == "pr"
