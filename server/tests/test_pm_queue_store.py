from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from server.db.models import (Base, GhProject, GhProjectItem, Project, ProjectRepo,
                              Session as DbSession, WorkItem, WorkItemDep)
from server.pm.queue_store import load_queue_rows

NOW = datetime(2026, 9, 4, tzinfo=timezone.utc)


def _db():
    e = create_engine("sqlite://")
    Base.metadata.create_all(e)
    return Session(e)


def _seed_product(db, slug, repo, *, board=True):
    project = Project(slug=slug, name=slug)
    db.add(project)
    db.flush()
    db.add(ProjectRepo(project_id=project.id, repo=repo))
    if board:
        gh = GhProject(project_id=project.id, gh_node_id=f"PVT_{slug}", gh_project_number=1, title=slug)
        db.add(gh)
    db.flush()
    return project


def _item(db, repo, number, *, status, score=None, is_epic=False, state="open"):
    w = WorkItem(repo=repo, number=number, kind="issue", state=state, title=f"issue {number}",
                body="body", url=f"https://github.com/{repo}/issues/{number}", author="steiner385",
                labels=[], assignees=[], is_epic=is_epic, gh_created_at=NOW, gh_updated_at=NOW)
    db.add(w)
    db.flush()
    project_repo = db.query(ProjectRepo).filter_by(repo=repo).first()
    gh = db.query(GhProject).filter_by(project_id=project_repo.project_id).first() if project_repo else None
    if gh is not None:
        db.add(GhProjectItem(gh_project_id=gh.id, work_item_id=w.id,
                             gh_item_node_id=f"PVTI_{repo}_{number}", status=status, score=score))
    db.flush()
    return w


def test_load_queue_rows_joins_board_status_blockers_sessions_and_children():
    db = _db()
    _seed_product(db, "ccswitch", "steiner385/ccswitch", board=True)
    a = _item(db, "steiner385/ccswitch", 1, status="Ready", score=8.0)
    _item(db, "steiner385/ccswitch", 2, status="Ready")
    c = _item(db, "steiner385/ccswitch", 3, status="Ready")  # blocked by open #1
    epic = _item(db, "steiner385/ccswitch", 4, status="Backlog", is_epic=True)
    child = _item(db, "steiner385/ccswitch", 5, status="Ready")
    child.parent_work_item_id = epic.id
    db.add(WorkItemDep(src_ref="steiner385/ccswitch#3", dst_ref="steiner385/ccswitch#1", source="native",
                       active=True, src_work_item_id=c.id, dst_work_item_id=a.id))
    db.add(WorkItemDep(src_ref="steiner385/ccswitch#2", dst_ref="cairnea/KinDash#7", source="prose",
                       active=True))  # unmirrored cross-repo dst
    db.add(DbSession(id="s1", name="s", account_name="a", cwd="/tmp", origin="backlog-runner",
                     origin_ref="steiner385/ccswitch#2", status="in_progress"))
    db.commit()

    rows = {r.ref: r for r in load_queue_rows(db, now=NOW, claimed_refs={"steiner385/ccswitch#5"})}

    assert rows["steiner385/ccswitch#3"].open_blockers == ("steiner385/ccswitch#1",)
    assert rows["steiner385/ccswitch#2"].open_blockers == ("cairnea/KinDash#7",)  # unknown state fails closed
    assert rows["steiner385/ccswitch#2"].live_session is True
    assert rows["steiner385/ccswitch#4"].open_children == 1
    assert rows["steiner385/ccswitch#5"].claimed is True
    assert rows["steiner385/ccswitch#1"].product == "ccswitch" and rows["steiner385/ccswitch#1"].score == 8.0


def test_open_blocker_resolves_dst_across_products_and_closed_releases_it():
    """A dep's dst_ref may live in a different product's repo entirely (#999: dst
    resolution is unscoped across products). Seed a second product whose mirrored
    WorkItem is the dst of two deps from the first product: one to a CLOSED issue
    (releases the blocker) and one to an OPEN issue (still blocks)."""
    db = _db()
    _seed_product(db, "ccswitch", "steiner385/ccswitch", board=True)
    _seed_product(db, "kindash", "cairnea/KinDash", board=False)  # dst-only product, no board
    src_closed_dst = _item(db, "steiner385/ccswitch", 10, status="Ready")
    src_open_dst = _item(db, "steiner385/ccswitch", 11, status="Ready")
    _item(db, "cairnea/KinDash", 20, status="Ready", state="closed")  # closed blocker
    _item(db, "cairnea/KinDash", 21, status="Ready", state="open")  # open blocker
    db.add(WorkItemDep(src_ref="steiner385/ccswitch#10", dst_ref="cairnea/KinDash#20", source="native",
                       active=True, src_work_item_id=src_closed_dst.id))
    db.add(WorkItemDep(src_ref="steiner385/ccswitch#11", dst_ref="cairnea/KinDash#21", source="native",
                       active=True, src_work_item_id=src_open_dst.id))
    db.commit()

    rows = {r.ref: r for r in load_queue_rows(db, now=NOW)}

    assert rows["steiner385/ccswitch#10"].open_blockers == ()  # closed dst releases the blocker
    assert rows["steiner385/ccswitch#11"].open_blockers == ("cairnea/KinDash#21",)  # open dst still blocks


def test_load_queue_rows_skips_products_without_a_board_and_closed_items():
    db = _db()
    _seed_product(db, "ccswitch", "steiner385/ccswitch", board=True)
    _seed_product(db, "kindash", "cairnea/KinDash", board=False)
    _item(db, "steiner385/ccswitch", 1, status="Ready", score=5.0)
    _item(db, "steiner385/ccswitch", 2, status="Ready", state="closed")
    _item(db, "cairnea/KinDash", 3, status="Ready", score=9.0)
    db.commit()

    rows = {r.ref: r for r in load_queue_rows(db, now=NOW)}

    assert set(rows) == {"steiner385/ccswitch#1"}
