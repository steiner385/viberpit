from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from server.db.models import (
    Base, GhProject, GhProjectItem, Initiative, Project, ProjectRepo, WorkItem, WorkItemDep)
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


def test_upsert_relations_backfills_fks_when_target_later_mirrored():
    """FKs resolve to NULL at creation when the target isn't mirrored yet; a
    later pass — once repo-sync catches up — must backfill them (issue #973)."""
    db = _db()
    _seed(db)
    rels = [IssueRelView(repo=REPO, number=950, blocked_by=[f"{REPO}#949"],
                         parent_ref=None, sub_issue_refs=[])]
    mirror_store.upsert_relations(db, REPO, rels)
    dep = db.query(WorkItemDep).filter_by(source="native").one()
    assert dep.dst_work_item_id is None  # #949 not mirrored yet
    # repo-sync catches up: #949 lands in work_items.
    db.add(WorkItem(repo=REPO, number=949, kind="issue", state="open"))
    db.commit()
    # A refresh pass with the SAME observed edge backfills the FK (no reactivate,
    # the row stayed active) — exercises the refresh path, not just reactivate.
    mirror_store.upsert_relations(db, REPO, rels)
    dep = db.query(WorkItemDep).filter_by(source="native").one()
    assert dep.dst_work_item_id == db.query(WorkItem).filter_by(number=949).one().id


def test_upsert_relations_stores_node_ids():
    db = _db()
    _seed(db)
    mirror_store.upsert_relations(db, REPO, [IssueRelView(
        repo=REPO, number=945, blocked_by=[], parent_ref=None, sub_issue_refs=[],
        node_id="I_945")])
    assert db.query(WorkItem).filter_by(number=945).one().gh_node_id == "I_945"


def test_upsert_relations_only_touches_the_given_repo():
    """The src_ref prefix filter must scope reconciliation to *this* repo — a dep
    row owned by another repo is never deactivated by an unrelated repo's pass."""
    db = _db()
    _seed(db)
    other = "steiner385/KinDash"
    db.add(WorkItemDep(src_ref=f"{other}#5", dst_ref=f"{other}#4",
                       source="native", active=True))
    db.commit()
    mirror_store.upsert_relations(db, REPO, [])  # REPO sees no edges
    foreign = db.query(WorkItemDep).filter_by(src_ref=f"{other}#5").one()
    assert foreign.active is True  # untouched by REPO's pass


def test_upsert_project_items_keys_multiple_items_by_node_id():
    """Preloaded existing-items dict must key correctly across many items and
    still upsert-in-place on a second pass (no duplicate rows)."""
    db = _db()
    project = _seed(db)
    gid = mirror_store.upsert_gh_project(db, project.id, GhProjectView(
        node_id="PVT_1", number=7, title="x", updated_at=None))
    now = datetime.now(timezone.utc)
    items = [
        GhProjectItemView(item_node_id="PVTI_1", repo=REPO, number=950, kind="issue",
                          status="Ready", score=None, stage=None, updated_at=None),
        GhProjectItemView(item_node_id="PVTI_2", repo=REPO, number=951, kind="issue",
                          status="Blocked", score=None, stage=None, updated_at=None),
    ]
    mirror_store.upsert_project_items(db, gid, items, now)
    assert db.query(GhProjectItem).count() == 2
    # second pass flips a status in place — no new rows.
    items[0] = GhProjectItemView(item_node_id="PVTI_1", repo=REPO, number=950,
                                 kind="issue", status="Done", score=None,
                                 stage=None, updated_at=None)
    mirror_store.upsert_project_items(db, gid, items, now)
    assert db.query(GhProjectItem).count() == 2
    assert db.query(GhProjectItem).filter_by(gh_item_node_id="PVTI_1").one().status == "Done"


def test_echo_suppression_distinguishes_own_writes_from_human_edits():
    """A field change matching a recent own-write (journal shape) is our own
    write echoing back through the GitHub read path — not a human edit. A
    brand-new row also produces no edits (nothing to have "changed" yet)."""
    db = _db()
    project = _seed(db)
    gid = mirror_store.upsert_gh_project(db, project.id, GhProjectView(
        node_id="PVT_1", number=7, title="x", updated_at=None))
    now = datetime.now(timezone.utc)
    first = [GhProjectItemView(item_node_id="PVTI_1", repo=REPO, number=950,
                               kind="issue", status="Backlog", score=None,
                               stage=None, updated_at=None)]
    no_edits = mirror_store.upsert_project_items(db, gid, first, now)
    assert no_edits == []  # brand-new row: nothing classified
    changed = [GhProjectItemView(item_node_id="PVTI_1", repo=REPO, number=950,
                                 kind="issue", status="Ready", score=480.0,
                                 stage=None, updated_at=None)]
    edits = mirror_store.upsert_project_items(
        db, gid, changed, now, own_writes={("PVTI_1", "Status", '"Ready"')})
    assert [(e.field, e.old, e.new) for e in edits] == [("Score", None, 480.0)]


def test_echo_suppression_matches_a_score_github_rounds_to_an_int():
    """We write Score 480.0; GitHub can hand it back as 480. Both sides key through
    journal.own_write_key, which normalizes numbers, so it is still an echo."""
    from server.pm import journal
    db = _db()
    project = _seed(db)
    gid = mirror_store.upsert_gh_project(db, project.id, GhProjectView(
        node_id="PVT_1", number=7, title="x", updated_at=None))
    now = datetime.now(timezone.utc)
    mirror_store.upsert_project_items(db, gid, [GhProjectItemView(
        item_node_id="PVTI_1", repo=REPO, number=950, kind="issue", status=None,
        score=None, stage=None, updated_at=None)], now)
    echoed = [GhProjectItemView(item_node_id="PVTI_1", repo=REPO, number=950,
                                kind="issue", status=None, score=480, stage=None,
                                updated_at=None)]
    own = {journal.own_write_key("PVTI_1", "Score", 480.0)}   # what the journal produces
    assert mirror_store.upsert_project_items(db, gid, echoed, now, own_writes=own) == []


def test_back_sync_stage_updates_linked_initiative():
    """A human "Stage" edit on a linked epic back-syncs Initiative.stage; an
    unrecognized new value (not in INITIATIVE_STAGES) is rejected — count 0,
    no write."""
    db = _db()
    _seed(db)
    db.add(Initiative(slug="i", stage="plan", epic_repo=REPO, epic_number=945))
    db.commit()
    n = mirror_store.back_sync_stage(
        db, [mirror_store.HumanEdit(f"{REPO}#945", "Stage", "plan", "build")])
    assert n == 1 and db.query(Initiative).one().stage == "build"
    assert mirror_store.back_sync_stage(
        db, [mirror_store.HumanEdit(f"{REPO}#945", "Stage", "build", "bogus")]) == 0
