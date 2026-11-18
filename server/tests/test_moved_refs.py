"""The moved-issue map (docs/superpowers/specs/2026-11-18-product-repo-lifecycle-design.md,
"Moved-issue map: map and migrate"): validation, idempotent migration of the
ViberPit-owned state, title-match suggestions, and old-ref resolution in the milestone
and blocker readers."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from server.db.models import (
    AuditEvent, BacklogGroup, Base, MilestoneLadder, MilestoneNote, MovedIssueRef,
    OwnerRecommendation, Project, ProjectRepo, WorkItem, WorkItemDep)
from server.pm import moved_refs
from server.pm.holds import set_decision_hold, set_hold
from server.pm.moved_refs import BadPairs

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
OLD_REPO, NEW_REPO = "o/ghost-mcp", "o/ghost"
OLD, NEW = f"{OLD_REPO}#257", f"{NEW_REPO}#3"


def _db() -> Session:
    e = create_engine("sqlite://")
    Base.metadata.create_all(e)
    return Session(e)


def _product(db: Session, *, retire_old: bool = True) -> str:
    p = Project(slug="ghost", name="Ghost")
    db.add(p)
    db.flush()
    db.add(ProjectRepo(project_id=p.id, repo=NEW_REPO, is_primary=True))
    db.add(ProjectRepo(project_id=p.id, repo=OLD_REPO, is_primary=False,
                       retired_at=NOW if retire_old else None))
    db.flush()
    return p.id


def _wi(db: Session, ref: str, *, title: str = "t", state: str = "open", **kw) -> WorkItem:
    repo, _, n = ref.rpartition("#")
    w = WorkItem(repo=repo, number=int(n), kind="issue", state=state, title=title, body="b", **kw)
    db.add(w)
    db.flush()
    return w


def _pair(db: Session, pid: str, old: str = OLD, new: str = NEW) -> MovedIssueRef:
    pairs = moved_refs.validate_pairs(db, pid, [(old, new)])
    (row,) = moved_refs.record_pairs(db, pid, pairs, actor="tony@example.com")
    return row


def _audits(db: Session, type_: str) -> list[AuditEvent]:
    return list(db.scalars(select(AuditEvent).where(AuditEvent.type == type_)))


# --------------------------------------------------------------------------- overlay + hold


def test_migrate_copies_overlay_into_empty_fields_and_moves_the_hold():
    db = _db()
    pid = _product(db)
    old = _wi(db, OLD, reach=3, impact=2, effort=5, confidence=80, triage_state="classified",
              classified_by="tony@example.com", classified_at=NOW)
    set_hold(old, kind="owner_action", reason="needs a call", actor="tony@example.com", now=NOW)
    new = _wi(db, NEW, reach=9)  # its own reach wins; triage_state is the "unclassified" default
    row = _pair(db, pid)

    detail = moved_refs.migrate_pair(db, row, now=NOW)

    assert detail is not None and detail["status"] == "migrated"
    assert (new.reach, new.impact, new.effort, new.confidence) == (9, 2, 5, 80)
    assert new.triage_state == "classified" and new.classified_by == "tony@example.com"
    assert "reach" not in detail["overlay"] and "impact" in detail["overlay"]
    assert (new.hold_kind, new.hold_reason, new.hold_by) == (
        "owner_action", "needs a call", "tony@example.com")
    assert new.hold_fingerprint is not None
    assert old.hold_kind is None and old.hold_reason is None  # no longer shows as held
    assert detail["hold"] == {"kind": "owner_action", "copied": True, "decision": False}
    assert row.migrated_at is not None and row.migration == detail
    (release,) = _audits(db, "pm.hold.release")
    assert release.detail["signal"] == "moved" and release.detail["kind"] == "owner_action"
    (mig,) = _audits(db, "pm.moved_ref.migrate")
    assert mig.detail["old"] == OLD and mig.detail["new"] == NEW


def test_decision_hold_is_copied_as_is_and_cleared_on_the_old_item():
    db = _db()
    pid = _product(db)
    old = _wi(db, OLD)
    set_decision_hold(old, reason="pick an option", actor="tony@example.com", now=NOW)
    new = _wi(db, NEW)
    detail = moved_refs.migrate_pair(db, _pair(db, pid), now=NOW)
    assert detail is not None
    assert new.hold_kind == "decision" and new.hold_reason == "pick an option"
    assert old.hold_kind is None
    assert detail["hold"] == {"kind": "decision", "copied": True, "decision": True}


def test_old_hold_released_even_when_the_new_item_already_holds():
    db = _db()
    pid = _product(db)
    old = _wi(db, OLD)
    set_hold(old, kind="owner_action", reason="old", actor="tony@example.com", now=NOW)
    new = _wi(db, NEW)
    set_hold(new, kind="evidence", reason="new", actor="tony@example.com", now=NOW)
    detail = moved_refs.migrate_pair(db, _pair(db, pid), now=NOW)
    assert detail is not None and detail["hold"]["copied"] is False
    assert (new.hold_kind, new.hold_reason) == ("evidence", "new")
    assert old.hold_kind is None


# --------------------------------------------------------------------------- deps


def test_deps_are_rewritten_and_a_duplicate_edge_is_deactivated():
    db = _db()
    pid = _product(db)
    old, new = _wi(db, OLD), _wi(db, NEW)
    x, y = _wi(db, "o/ghost#10"), _wi(db, "o/ghost#11")
    blocks_old = WorkItemDep(src_ref="o/ghost#10", dst_ref=OLD, source="prose", active=True,
                             src_work_item_id=x.id, dst_work_item_id=old.id)
    dup_existing = WorkItemDep(src_ref="o/ghost#10", dst_ref=NEW, source="prose", active=True,
                               src_work_item_id=x.id, dst_work_item_id=new.id)
    old_blocked = WorkItemDep(src_ref=OLD, dst_ref="o/ghost#11", source="native", active=True,
                              src_work_item_id=old.id, dst_work_item_id=y.id)
    native_on_old = WorkItemDep(src_ref="o/ghost#11", dst_ref=OLD, source="native", active=True,
                                src_work_item_id=y.id, dst_work_item_id=old.id)
    db.add_all([blocks_old, dup_existing, old_blocked, native_on_old])
    db.flush()

    detail = moved_refs.migrate_pair(db, _pair(db, pid), now=NOW)
    db.flush()  # the unique (src, dst, source) constraint holds

    assert detail is not None and detail["deps"] == {"rewritten": 2, "deactivated": 1}
    assert blocks_old.active is False and blocks_old.dst_ref == OLD  # deduped, not duplicated
    assert dup_existing.active is True
    assert (old_blocked.src_ref, old_blocked.src_work_item_id) == (NEW, new.id)
    assert (native_on_old.dst_ref, native_on_old.dst_work_item_id) == (NEW, new.id)


# --------------------------------------------------------------------------- notes, recs, groups


def test_open_notes_and_pending_recommendations_follow_and_groups_are_dropped():
    db = _db()
    pid = _product(db)
    old, new = _wi(db, OLD), _wi(db, NEW)
    ladder = MilestoneLadder(project_id=pid, title="t")
    db.add(ladder)
    db.flush()
    open_ask = MilestoneNote(ladder_id=ladder.id, kind="ask", ref=OLD, status="open")
    done_ask = MilestoneNote(ladder_id=ladder.id, kind="ask", ref=OLD, status="done")
    pending = OwnerRecommendation(project_id=pid, rule="r1", target_ref=OLD, action="a",
                                  work_item_id=old.id, status="pending")
    dup = OwnerRecommendation(project_id=pid, rule="r2", target_ref=OLD, action="a",
                              status="pending")
    already_on_new = OwnerRecommendation(project_id=pid, rule="r2", target_ref=NEW, action="a",
                                         status="pending")
    applied = OwnerRecommendation(project_id=pid, rule="r3", target_ref=OLD, action="a",
                                  status="applied")
    db.add_all([open_ask, done_ask, pending, dup, already_on_new, applied])
    db.add_all([BacklogGroup(generation="gen-1", rank=0, refs=["o/ghost#9", OLD]),
                BacklogGroup(generation="gen-1", rank=1, refs=["o/ghost#8"])])
    db.flush()

    detail = moved_refs.migrate_pair(db, _pair(db, pid), now=NOW)

    assert detail is not None
    assert open_ask.ref == NEW and done_ask.ref == OLD
    assert (pending.target_ref, pending.work_item_id) == (NEW, new.id)
    assert dup.status == "superseded" and dup.target_ref == OLD
    assert applied.target_ref == OLD
    assert [g.refs for g in db.scalars(select(BacklogGroup))] == [["o/ghost#8"]]
    assert (detail["notes"], detail["recommendations"], detail["groupsDropped"]) == (1, 2, 1)


# --------------------------------------------------------------------------- Review Focus 4


def test_pair_with_unmirrored_new_item_is_pending_then_migrates_once():
    db = _db()
    pid = _product(db)
    old = _wi(db, OLD, reach=4)
    set_hold(old, kind="owner_action", reason="r", actor="tony@example.com", now=NOW)
    row = _pair(db, pid)

    assert moved_refs.migrate_pair(db, row, now=NOW) is None
    assert row.migrated_at is None
    assert row.migration == {"status": "pending", "missing": [NEW]}
    assert old.hold_kind == "owner_action"  # nothing moved while pending
    assert moved_refs.migrate_pending(db, now=NOW) == 0

    new = _wi(db, NEW)  # a later mirror pass brings the new issue in
    assert moved_refs.migrate_pending(db, project_id=pid, now=NOW) == 1
    assert row.migrated_at is not None and new.reach == 4 and new.hold_kind == "owner_action"

    first = dict(row.migration or {})
    assert moved_refs.migrate_pending(db, now=NOW) == 0          # idempotent re-run
    assert moved_refs.migrate_pair(db, row, now=NOW) == first   # no second migration
    assert len(_audits(db, "pm.moved_ref.migrate")) == 1
    assert len(_audits(db, "pm.hold.release")) == 1


# --------------------------------------------------------------------------- validation


def test_validation_rejects_chains_bad_refs_and_foreign_or_retired_repos():
    db = _db()
    pid = _product(db)
    _pair(db, pid)  # OLD -> NEW stored

    with pytest.raises(BadPairs) as e:
        moved_refs.validate_pairs(db, pid, [(NEW, "o/ghost#4")])  # NEW is already a new ref
    assert "chained" in e.value.errors[0]["message"]
    with pytest.raises(BadPairs) as e:
        moved_refs.validate_pairs(db, pid, [("o/ghost#5", OLD)])  # OLD is retired
    assert "not an active repo" in e.value.errors[0]["message"]
    with pytest.raises(BadPairs) as e:  # a chain inside one batch
        moved_refs.validate_pairs(db, pid, [("o/ghost-mcp#1", "o/ghost#1"),
                                            ("o/ghost#1", "o/ghost#2")])
    assert any("chained" in x["message"] for x in e.value.errors)
    with pytest.raises(BadPairs) as e:
        moved_refs.validate_pairs(db, pid, [("nope", NEW), ("x/y#1", "o/ghost#9"),
                                            ("o/ghost-mcp#2", "o/ghost-mcp#3")])
    assert [x["message"] for x in e.value.errors] == [
        "refs must look like owner/name#N",
        "x/y is not a repo of this product",
        "o/ghost-mcp is not an active repo of this product"]
    with pytest.raises(BadPairs) as e:
        moved_refs.validate_pairs(db, pid, [("o/ghost-mcp#7", "o/ghost#7"),
                                            ("o/ghost-mcp#7", "o/ghost#8")])
    assert "more than once" in e.value.errors[0]["message"]

    # Re-recording the stored pair is not a chain, and repo case is canonicalised.
    assert moved_refs.validate_pairs(db, pid, [("O/Ghost-MCP#257", "O/GHOST#3")]) == [(OLD, NEW)]


def test_repointing_a_pair_resets_its_migration():
    db = _db()
    pid = _product(db)
    _wi(db, OLD)
    _wi(db, NEW)
    row = _pair(db, pid)
    moved_refs.migrate_pair(db, row, now=NOW)
    assert row.migrated_at is not None
    (again,) = moved_refs.record_pairs(db, pid, [(OLD, "o/ghost#4")], actor="tony@example.com")
    assert again is row and row.new_ref == "o/ghost#4" and row.migrated_at is None


# --------------------------------------------------------------------------- suggest


def test_suggest_matches_folded_titles_and_skips_ambiguous_closed_and_mapped():
    db = _db()
    pid = _product(db)
    _wi(db, "o/ghost-mcp#1", title="Fix   the  Login flow")
    _wi(db, "o/ghost#11", title="fix the login FLOW", state="closed")  # any state on the new side
    _wi(db, "o/ghost-mcp#2", title="Dup")
    _wi(db, "o/ghost#12", title="dup")
    _wi(db, "o/ghost#13", title="DUP")                                 # ambiguous on the new side
    _wi(db, "o/ghost-mcp#3", title="Closed one", state="closed")       # old side: open only
    _wi(db, "o/ghost#14", title="closed one")
    _wi(db, "o/ghost-mcp#4", title="Already mapped")
    _wi(db, "o/ghost#15", title="already mapped")
    _wi(db, "o/ghost-mcp#5", title="No twin")
    _pair(db, pid, "o/ghost-mcp#4", "o/ghost#15")

    assert moved_refs.suggest_pairs(db, OLD_REPO, NEW_REPO) == [
        {"old": "o/ghost-mcp#1", "new": "o/ghost#11", "title": "Fix   the  Login flow"}]


# --------------------------------------------------------------------------- readers


def test_resolve_moved_and_map():
    db = _db()
    pid = _product(db)
    _pair(db, pid)
    moved = moved_refs.moved_map(db)
    assert moved == {OLD: NEW}
    assert moved_refs.resolve_moved(OLD, moved) == NEW
    assert moved_refs.resolve_moved("o/ghost#1", moved) == "o/ghost#1"
    assert moved_refs.resolve_moved(OLD, {OLD: NEW, NEW: OLD}) == NEW  # a cycle stops


def test_blocker_naming_the_old_ref_reads_the_new_issue_state():
    from server.pm.mirror_store import blocker_states, blocker_states_stmt
    db = _db()
    pid = _product(db)
    _wi(db, OLD, state="open")    # the archived repo's copy never closes
    _wi(db, NEW, state="closed")  # the live issue was fixed
    _pair(db, pid)
    moved = moved_refs.moved_map(db)

    plain = blocker_states(db.execute(blocker_states_stmt({OLD})).all())
    assert plain == {OLD: "open"}
    resolved = blocker_states(db.execute(blocker_states_stmt({OLD}, moved)).all(), moved)
    assert resolved[OLD] == "closed"


def test_queue_blocker_resolves_through_the_map():
    from server.db.models import GhProject, GhProjectItem
    from server.pm.queue_store import load_queue_rows
    db = _db()
    pid = _product(db)
    db.add(GhProject(project_id=pid, gh_node_id="PVT_g", gh_project_number=1, title="g"))
    db.flush()
    gh = db.scalars(select(GhProject)).one()
    _wi(db, OLD, state="open")
    _wi(db, NEW, state="closed")
    src = _wi(db, "o/ghost#20")
    db.add(GhProjectItem(gh_project_id=gh.id, work_item_id=src.id, gh_item_node_id="PVTI_1",
                         status="Ready"))
    db.add(WorkItemDep(src_ref="o/ghost#20", dst_ref=OLD, source="prose", active=True))
    db.commit()
    rows = {r.ref: r for r in load_queue_rows(db, now=NOW, claimed_refs=set())}
    assert rows["o/ghost#20"].open_blockers == (OLD,)  # no map: the old copy is open
    _pair(db, pid)
    db.commit()
    rows = {r.ref: r for r in load_queue_rows(db, now=NOW, claimed_refs=set())}
    assert rows["o/ghost#20"].open_blockers == ()


def test_milestone_refs_resolve_through_the_map():
    from server.pm.milestone_def import Ref, from_json, parse_ladder, parse_ref, to_json
    moved = {OLD: NEW}
    assert parse_ref("#257", primary_repo=OLD_REPO, moved=moved) == Ref(NEW_REPO, 3)
    assert parse_ref("#257", primary_repo=OLD_REPO) == Ref(OLD_REPO, 257)
    ladder = parse_ladder(f"""
version: 1
title: t
milestones:
  - key: one
    name: One
    epics: ['{OLD}']
    refs: ['{OLD}', 'o/ghost#1']
    gate:
      - key: g
        label: G
        measure: {{kind: refs_closed, refs: ['{OLD}']}}
""", primary_repo=NEW_REPO)
    resolved = from_json(to_json(ladder), moved=moved)
    (m,) = resolved.milestones
    assert m.epics == (Ref(NEW_REPO, 3),)
    assert m.refs == (Ref(NEW_REPO, 3), Ref(NEW_REPO, 1))
    assert m.gate[0].measure.params["refs"] == (Ref(NEW_REPO, 3),)
    assert from_json(to_json(ladder)).milestones[0].refs[0] == Ref(OLD_REPO, 257)


@pytest.mark.asyncio
async def test_milestone_refresh_load_reads_the_map(isolated_db):
    from server.db import db_session
    from server.pm.milestone_def import Ref, parse_ladder, to_json
    from server.pm.milestone_refresh import _load
    ladder = parse_ladder(f"""
version: 1
title: t
milestones:
  - key: one
    name: One
    refs: ['{OLD}']
""", primary_repo=NEW_REPO)
    async with db_session() as db:
        p = Project(slug="ghost", name="Ghost")
        db.add(p)
        await db.flush()
        db.add(ProjectRepo(project_id=p.id, repo=NEW_REPO, is_primary=True))
        db.add(MilestoneLadder(project_id=p.id, definition=to_json(ladder), title="t"))
        db.add(MovedIssueRef(project_id=p.id, old_ref=OLD, new_ref=NEW))
        pid = p.id
    loaded = await _load(pid)
    assert loaded is not None
    assert loaded[1].milestones[0].refs == (Ref(NEW_REPO, 3),)


# --------------------------------------------------------------------------- pm_sync retry


@pytest.mark.asyncio
async def test_pm_sync_state_save_retries_pending_pairs(isolated_db):
    from server.db import db_session
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory
    async with db_session() as db:
        p = Project(slug="ghost", name="Ghost")
        db.add(p)
        await db.flush()
        db.add(ProjectRepo(project_id=p.id, repo=NEW_REPO, is_primary=True))
        db.add(WorkItem(repo=OLD_REPO, number=257, kind="issue", state="open", reach=7))
        db.add(MovedIssueRef(project_id=p.id, old_ref=OLD, new_ref=NEW))
        pid = p.id
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    view = SimpleNamespace(project_id=pid, repo=NEW_REPO)

    await orch._pm_sync_save_state(view, watermark=NOW)  # new issue not mirrored yet
    async with db_session() as db:
        row = (await db.execute(select(MovedIssueRef))).scalar_one()
        assert row.migrated_at is None and row.migration["status"] == "pending"
        db.add(WorkItem(repo=NEW_REPO, number=3, kind="issue", state="open"))

    await orch._pm_sync_save_state(view, backfill_cursor="c")  # no mirror advance: no retry
    async with db_session() as db:
        assert (await db.execute(select(MovedIssueRef))).scalar_one().migrated_at is None

    await orch._pm_sync_save_state(view, watermark=NOW)
    async with db_session() as db:
        assert (await db.execute(select(MovedIssueRef))).scalar_one().migrated_at is not None
        new = (await db.execute(select(WorkItem).where(WorkItem.number == 3))).scalar_one()
        assert new.reach == 7


# --------------------------------------------------------------------------- API


def test_moved_refs_api_records_migrates_lists_and_suggests(monkeypatch):
    from server.tests.pm_ownership_fixtures import identity_repositories
    from server.tests.test_pm_api import IDENTITY_HEADERS as H, _client
    monkeypatch.setattr("server.automations.target_identity.resolve_repositories",
                        identity_repositories)

    with _client() as c:
        resp = c.post("/api/pm/projects", json={"name": "Ghost", "repos": [
            {"repo": NEW_REPO, "isPrimary": True}, {"repo": OLD_REPO, "isPrimary": False}]},
            headers=H)
        assert resp.status_code == 201, resp.text
        resp = c.post(f"/api/pm/projects/ghost/repos/{OLD_REPO}/retire", json={}, headers=H)
        assert resp.status_code == 200, resp.text

        from server.db import db_session

        async def seed():
            async with db_session() as db:
                db.add(WorkItem(repo=OLD_REPO, number=257, kind="issue", state="open",
                                title="Login breaks", reach=5))
                db.add(WorkItem(repo=NEW_REPO, number=3, kind="issue", state="open",
                                title="login  breaks"))
                db.add(WorkItem(repo=OLD_REPO, number=258, kind="issue", state="open",
                                title="Not moved yet"))
        c.portal.call(seed)

        resp = c.get(f"/api/pm/projects/ghost/moved-refs/suggest?from={OLD_REPO}&to={NEW_REPO}",
                     headers=H)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"pairs": [{"old": OLD, "new": NEW, "title": "Login breaks"}]}
        resp = c.get(f"/api/pm/projects/ghost/moved-refs/suggest?from={NEW_REPO}&to={OLD_REPO}",
                     headers=H)
        assert resp.status_code == 422 and resp.json()["detail"]["error"] == "bad_repo"

        resp = c.post("/api/pm/projects/ghost/moved-refs", json={"pairs": [
            {"old": OLD, "new": NEW}, {"old": "o/ghost-mcp#258", "new": "o/ghost#4"}]}, headers=H)
        assert resp.status_code == 200, resp.text
        by_old = {p["old"]: p for p in resp.json()["pairs"]}
        assert by_old[OLD]["status"] == "migrated" and by_old[OLD]["migratedAt"]
        assert by_old[OLD]["migration"]["overlay"] == ["reach"]
        assert by_old["o/ghost-mcp#258"]["status"] == "pending"
        assert by_old["o/ghost-mcp#258"]["migratedAt"] is None

        listed = c.get("/api/pm/projects/ghost/moved-refs", headers=H)
        assert listed.status_code == 200 and listed.json() == resp.json()

        bad = c.post("/api/pm/projects/ghost/moved-refs", json={"pairs": [
            {"old": NEW, "new": "o/ghost#9"}]}, headers=H)
        assert bad.status_code == 422, bad.text
        detail = bad.json()["detail"]
        assert detail["error"] == "bad_pairs" and "chained" in detail["pairs"][0]["message"]

        async def audit_types():
            async with db_session() as db:
                return sorted((await db.execute(select(AuditEvent.type).where(
                    AuditEvent.type.like("pm.moved_ref.%")))).scalars().all())
        assert c.portal.call(audit_types) == ["pm.moved_ref.migrate", "pm.moved_ref.record"]
