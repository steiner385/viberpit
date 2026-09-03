from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from server.db.models import (Base, GhProject, GhProjectItem, Initiative, Project, ProjectRepo,
                              Session as DbSession, WorkItem, WorkItemDep)
from server.pm.mirror_store import list_product_views
from server.pm.reconcile_inputs import load_product_facts

REPO = "steiner385/cc-account-switcher"
T = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def _db():
    e = create_engine("sqlite://"); Base.metadata.create_all(e); return Session(e)


def _seed(db):
    p = Project(slug="ccswitch", name="ccswitch"); db.add(p); db.flush()
    db.add(ProjectRepo(project_id=p.id, repo=REPO))
    gp = GhProject(project_id=p.id, gh_node_id="PVT_1", gh_project_number=26, title="ccswitch",
                   field_cache={"Status": {"field_id": "F", "options": {}}}, last_synced_at=T)
    db.add(gp); db.flush()
    i5 = WorkItem(repo=REPO, number=5, kind="issue", state="open", gh_node_id="I_5", triage_state="classified",
                  classified_at=T, reach=4, impact=3, effort=2, confidence=80, labels=[])
    i3 = WorkItem(repo=REPO, number=3, kind="issue", state="open", gh_node_id="I_3", labels=["needs-human"])
    i9 = WorkItem(repo=REPO, number=9, kind="issue", state="open", gh_node_id="I_9", labels=[])
    pr = WorkItem(repo=REPO, number=50, kind="pr", state="merged", linked_issue_numbers=[9], gh_closed_at=T, labels=[])
    db.add_all([i5, i3, i9, pr]); db.flush()
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
    assert f3.needs_human_label is True and f3.session_status == "waiting_question"
    assert f9.merged_pr_at == T
    assert set(facts.mirror) == {f"{REPO}#5"} and facts.mirror[f"{REPO}#5"].item_node_id == "PVTI_5"
    assert facts.open_issue_nodes == {f"{REPO}#5": "I_5", f"{REPO}#3": "I_3", f"{REPO}#9": "I_9"}
    assert [(d.src_ref, d.dst_ref, d.source) for d in facts.deps] == [(f"{REPO}#5", f"{REPO}#3", "prose")]
    assert facts.newest_decision_at == T and facts.mirror_synced_at == T
