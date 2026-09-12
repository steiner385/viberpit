from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from server.db.models import (Automation, AutomationTargetState, Base, GhProject, GhProjectItem,
                              Initiative, Project, ProjectRepo, Session as DbSession, WorkItem,
                              WorkItemDep)
from server.pm.mirror_store import list_product_views
from server.pm.reconcile_inputs import load_product_facts

REPO = "steiner385/viberpit"
T = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def _db():
    e = create_engine("sqlite://"); Base.metadata.create_all(e); return Session(e)


def _seed(db):
    p = Project(slug="viberpit", name="viberpit"); db.add(p); db.flush()
    db.add(ProjectRepo(project_id=p.id, repo=REPO))
    gp = GhProject(project_id=p.id, gh_node_id="PVT_1", gh_project_number=26, title="viberpit",
                   field_cache={"Status": {"field_id": "F", "options": {}}}, last_synced_at=T)
    db.add(gp); db.flush()
    i5 = WorkItem(repo=REPO, number=5, kind="issue", state="open", gh_node_id="I_5", triage_state="classified",
                  classified_at=T, reach=4, impact=3, effort=2, confidence=80, labels=[])
    i3 = WorkItem(repo=REPO, number=3, kind="issue", state="open", gh_node_id="I_3", labels=[])
    i4 = WorkItem(repo=REPO, number=4, kind="issue", state="open", gh_node_id="I_4", labels=[])
    i9 = WorkItem(repo=REPO, number=9, kind="issue", state="open", gh_node_id="I_9", labels=[])
    pr = WorkItem(repo=REPO, number=50, kind="pr", state="merged", linked_issue_numbers=[9], gh_closed_at=T, labels=[])
    db.add_all([i5, i3, i4, i9, pr]); db.add(Automation(id="backlog-runner")); db.flush()
    db.add(GhProjectItem(gh_project_id=gp.id, work_item_id=i5.id, gh_item_node_id="PVTI_5",
                         status="Backlog", gh_updated_at=T))
    db.add(WorkItemDep(src_work_item_id=i5.id, dst_work_item_id=i3.id, src_ref=f"{REPO}#5",
                       dst_ref=f"{REPO}#3", source="prose", active=True, updated_at=T))
    db.add(DbSession(name="s", account_name="a", cwd="/tmp", repo=REPO, origin="backlog-runner",
                     origin_ref=f"{REPO}#3", status="waiting_question", updated_at=T))
    db.add(Initiative(slug="x", stage="build", epic_repo=REPO, epic_number=5, updated_at=T))
    db.commit()
    return p


def test_load_product_facts_assembles_signals():
    db = _db(); _seed(db)
    product = list_product_views(db)[0]
    facts = load_product_facts(db, product, now=T)
    assert facts.project_node_id == "PVT_1" and facts.owner == "steiner385"
    by_ref = {f.ref: f for f in facts.items}
    f5, f3, f9 = by_ref[f"{REPO}#5"], by_ref[f"{REPO}#3"], by_ref[f"{REPO}#9"]
    assert (f5.triage_state, f5.reach, f5.initiative_stage) == ("classified", 4, "build")
    assert f5.open_blocker_refs == (f"{REPO}#3",) and f5.blockers_at == T
    assert f3.session_status == "waiting_question" and f3.needs_human_at is None
    assert f9.merged_pr_at == T
    assert set(facts.mirror) == {f"{REPO}#5"} and facts.mirror[f"{REPO}#5"].item_node_id == "PVTI_5"
    assert facts.open_issue_nodes == {f"{REPO}#5": "I_5", f"{REPO}#3": "I_3", f"{REPO}#4": "I_4",
                                      f"{REPO}#9": "I_9"}
    assert [(d.src_ref, d.dst_ref, d.source) for d in facts.deps] == [(f"{REPO}#5", f"{REPO}#3", "prose")]
    assert facts.newest_decision_at == T and facts.mirror_synced_at == T


def test_a_program_initiative_yields_no_stage_field():
    """A program (an initiative with phases) is never driven directly: its Stage
    is derived from its phases, so the reconciler must not push the program's own
    raw column stage — that would fight the UI's derived value and back_sync_stage's
    refusal to write a human's correction onto it."""
    db = _db(); _seed(db)
    program = Initiative(slug="program", stage="build", epic_repo=REPO, epic_number=6, updated_at=T)
    db.add(program); db.flush()
    db.add(Initiative(slug="phase", stage="deploy", epic_repo=REPO, epic_number=7,
                      parent_initiative_id=program.id, updated_at=T))
    i6 = WorkItem(repo=REPO, number=6, kind="issue", state="open", gh_node_id="I_6", labels=[])
    i7 = WorkItem(repo=REPO, number=7, kind="issue", state="open", gh_node_id="I_7", labels=[])
    db.add_all([i6, i7]); db.commit()

    facts = load_product_facts(db, list_product_views(db)[0], now=T)
    by_ref = {f.ref: f for f in facts.items}
    assert by_ref[f"{REPO}#6"].initiative_stage is None       # program: no Stage decision
    assert by_ref[f"{REPO}#7"].initiative_stage == "deploy"   # phase: unaffected


def test_a_runner_give_up_row_marks_every_issue_in_the_bundle():
    """Phase D: ``Needs human`` comes from the runner's durable give-up row, not a
    label. The row rides on the bundle's primary target, so every issue the runner
    gave up on carries the same stamp — a bundle is one give-up, not one per issue."""
    db = _db(); _seed(db)
    at = datetime(2026, 9, 9, tzinfo=timezone.utc)
    db.add(AutomationTargetState(
        automation_id="backlog-runner", target=f"{REPO}#3",
        state={"gave_up": True, "needs_human_at": at.isoformat(),
               "issues": [f"{REPO}#3", f"{REPO}#4"]}))
    db.commit()

    by_ref = {f.ref: f for f in load_product_facts(db, list_product_views(db)[0], now=T).items}
    assert by_ref[f"{REPO}#3"].needs_human_at == at
    assert by_ref[f"{REPO}#4"].needs_human_at == at
    assert by_ref[f"{REPO}#5"].needs_human_at is None


def test_a_give_up_makes_the_mirror_stale():
    """A give-up is a DECISION — it flips the board to ``Needs human`` — so a row
    stamped after the last sync must make the mirror stale, exactly like a dep edge.
    Otherwise the reconciler plans that push against a board it has not re-read."""
    db = _db(); _seed(db)
    product = list_product_views(db)[0]
    assert load_product_facts(db, product, now=T).newest_decision_at == T   # no rows: unchanged

    later = datetime(2026, 9, 9, tzinfo=timezone.utc)
    db.add(AutomationTargetState(
        automation_id="backlog-runner", target=f"{REPO}#3",
        state={"gave_up": True, "needs_human_at": later.isoformat()}))
    db.commit()

    facts = load_product_facts(db, product, now=later)
    assert facts.newest_decision_at == later
    assert facts.mirror_synced_at < facts.newest_decision_at


def test_a_runner_row_without_a_give_up_is_not_needs_human():
    """A live claim is the runner working the issue, not a handoff to a human."""
    db = _db(); _seed(db)
    db.add(AutomationTargetState(automation_id="backlog-runner", target=f"{REPO}#3",
                                 state={"claimed": True, "attempts": 1}))
    db.commit()

    by_ref = {f.ref: f for f in load_product_facts(db, list_product_views(db)[0], now=T).items}
    assert by_ref[f"{REPO}#3"].needs_human_at is None


def test_a_dep_edge_change_makes_the_mirror_stale():
    """The freshness gate is only as good as ``newest_decision_at``. Adding or
    deactivating a blocker flips an item to/from Blocked, so a dep edge written after
    the last sync must make the mirror stale — otherwise the reconciler plans a
    Blocked/Ready push against a board it has not re-read since."""
    db = _db(); _seed(db)
    product = list_product_views(db)[0]
    assert load_product_facts(db, product, now=T).newest_decision_at == T

    later = datetime(2026, 9, 3, 13, 0, tzinfo=timezone.utc)
    dep = db.query(WorkItemDep).one()
    dep.active = False                      # the blocker is lifted — a real decision
    dep.updated_at = later
    db.commit()
    facts = load_product_facts(db, product, now=later)
    assert facts.newest_decision_at == later      # ... and the mirror (synced at T) is stale
    assert facts.mirror_synced_at < facts.newest_decision_at


def test_session_row_writes_do_not_hold_the_gate_permanently_stale():
    """``Session.updated_at`` is a generic row-write time, so folding it into
    ``newest_decision_at`` would pin it at ~now for as long as any session is live and
    the mirror could never catch up — every tick skipped_stale, nothing ever applied."""
    db = _db(); _seed(db)
    product = list_product_views(db)[0]
    sess = db.query(DbSession).one()
    sess.updated_at = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)   # a live session
    db.commit()
    facts = load_product_facts(db, product, now=T)
    assert facts.newest_decision_at == T
    assert not facts.mirror_synced_at < facts.newest_decision_at        # still convergeable


def test_deps_are_scoped_to_the_products_repos_in_sql():
    """The dep filter is a SQL predicate, not a Python scan of every dep ViberPit has
    ever observed — and it must not match a repo whose name merely shares a prefix."""
    db = _db(); p = _seed(db)
    other = WorkItem(repo="steiner385/other", number=1, kind="issue", state="open",
                     gh_node_id="I_o", labels=[])
    db.add(other); db.flush()
    db.add(WorkItemDep(src_work_item_id=other.id, dst_work_item_id=other.id,
                       src_ref="steiner385/other#1", dst_ref="steiner385/other#2",
                       source="prose", active=True, updated_at=T))
    # A repo whose name is a prefix of another ("...-switcher-fork") must not leak in.
    db.add(WorkItemDep(src_work_item_id=other.id, dst_work_item_id=other.id,
                       src_ref=f"{REPO}-fork#1", dst_ref=f"{REPO}-fork#2",
                       source="prose", active=True, updated_at=T))
    db.commit()
    facts = load_product_facts(db, list_product_views(db)[0], now=T)
    assert [d.src_ref for d in facts.deps] == [f"{REPO}#5"]
    assert p.slug == "viberpit"
