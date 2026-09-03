from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from server.db.models import (
    Base, GhProject, GhProjectItem, Project, ProjectRepo, WorkItem, WorkItemDep)
from server.pm.gh_projects import GhProjectItemView, GhProjectView, IssueRelView
from server.pm import mirror_store

REPO = "steiner385/cc-account-switcher"


def _db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed(db):
    project = Project(slug="ccswitch", name="cc-account-switcher")
    db.add(project)
    db.flush()
    db.add(ProjectRepo(project_id=project.id, repo=REPO))
    for number in (945, 950, 951):
        db.add(WorkItem(repo=REPO, number=number, kind="issue", state="open",
                        body="Depends on #949" if number == 950 else None))
    db.commit()
    return project


def test_upsert_gh_project_links_and_is_idempotent():
    db = _db()
    project = _seed(db)
    view = GhProjectView(node_id="PVT_1", number=7, title="cc-account-switcher",
                         updated_at="2026-09-01T10:00:00Z")
    gid1 = mirror_store.upsert_gh_project(db, project.id, view)
    gid2 = mirror_store.upsert_gh_project(db, project.id, view)
    assert gid1 == gid2
    assert db.query(GhProject).count() == 1
    views = mirror_store.list_product_views(db)
    assert views[0].gh_node_id == "PVT_1" and views[0].repos == [REPO]


def test_upsert_project_items_resolves_work_items_and_prunes_gone():
    db = _db()
    project = _seed(db)
    gid = mirror_store.upsert_gh_project(db, project.id, GhProjectView(
        node_id="PVT_1", number=7, title="x", updated_at=None))
    now = datetime.now(timezone.utc)
    items = [GhProjectItemView(item_node_id="PVTI_1", repo=REPO, number=950, kind="issue",
                               status="Ready", score=12.0, stage=None,
                               updated_at="2026-09-02T09:00:00Z")]
    mirror_store.upsert_project_items(db, gid, items, now)
    row = db.query(GhProjectItem).one()
    assert row.status == "Ready" and row.score == 12.0
    assert row.work_item_id == db.query(WorkItem).filter_by(number=950).one().id
    # second pass without the item removes the stale mirror row
    mirror_store.upsert_project_items(db, gid, [], now)
    assert db.query(GhProjectItem).count() == 0


def test_upsert_relations_sets_parents_and_plans_deps():
    db = _db()
    _seed(db)
    rels = [
        IssueRelView(repo=REPO, number=945, blocked_by=[], parent_ref=None,
                     sub_issue_refs=[f"{REPO}#950", f"{REPO}#951"]),
        IssueRelView(repo=REPO, number=950, blocked_by=["steiner385/KinDash#12"],
                     parent_ref=f"{REPO}#945", sub_issue_refs=[]),
    ]
    mirror_store.upsert_relations(db, REPO, rels)
    epic = db.query(WorkItem).filter_by(number=945).one()
    child = db.query(WorkItem).filter_by(number=950).one()
    assert epic.is_epic is True and child.parent_work_item_id == epic.id
    deps = {(d.src_ref, d.dst_ref, d.source, d.active)
            for d in db.query(WorkItemDep).all()}
    assert (f"{REPO}#950", "steiner385/KinDash#12", "native", True) in deps
    assert (f"{REPO}#950", f"{REPO}#949", "prose", True) in deps
    assert (f"{REPO}#951", f"{REPO}#950", "order", True) in deps
    # cross-repo target is unmirrored: dst_work_item_id stays None, dst_ref carries it
    native = [d for d in db.query(WorkItemDep).all() if d.source == "native"][0]
    assert native.dst_work_item_id is None
    # edge removed upstream -> deactivated, not deleted
    mirror_store.upsert_relations(db, REPO, [
        rels[0],
        IssueRelView(repo=REPO, number=950, blocked_by=[], parent_ref=f"{REPO}#945",
                     sub_issue_refs=[]),
    ])
    native = [d for d in db.query(WorkItemDep).all()
              if d.source == "native" and d.dst_ref == "steiner385/KinDash#12"][0]
    assert native.active is False
