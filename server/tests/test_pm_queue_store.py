from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from server.db.models import (Automation, AutomationTargetState, Base, DispatchOutcome,
                              GhProject, GhProjectItem, Project, ProjectRepo,
                              Session as DbSession, WorkItem, WorkItemDep)
from server.pm import queue_store
from server.pm.outcomes import body_fingerprint
from server.pm.queue_store import load_queue_rows, parse_iso_utc, runner_row_maps

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
    _seed_product(db, "viberpit", "steiner385/viberpit", board=True)
    a = _item(db, "steiner385/viberpit", 1, status="Ready", score=8.0)
    _item(db, "steiner385/viberpit", 2, status="Ready")
    c = _item(db, "steiner385/viberpit", 3, status="Ready")  # blocked by open #1
    epic = _item(db, "steiner385/viberpit", 4, status="Backlog", is_epic=True)
    child = _item(db, "steiner385/viberpit", 5, status="Ready")
    child.parent_work_item_id = epic.id
    db.add(WorkItemDep(src_ref="steiner385/viberpit#3", dst_ref="steiner385/viberpit#1", source="native",
                       active=True, src_work_item_id=c.id, dst_work_item_id=a.id))
    db.add(WorkItemDep(src_ref="steiner385/viberpit#2", dst_ref="cairnea/KinDash#7", source="prose",
                       active=True))  # unmirrored cross-repo dst
    db.add(DbSession(id="s1", name="s", account_name="a", cwd="/tmp", origin="backlog-runner",
                     origin_ref="steiner385/viberpit#2", status="in_progress"))
    db.commit()

    rows = {r.ref: r for r in load_queue_rows(db, now=NOW, claimed_refs={"steiner385/viberpit#5"})}

    assert rows["steiner385/viberpit#3"].open_blockers == ("steiner385/viberpit#1",)
    assert rows["steiner385/viberpit#2"].open_blockers == ("cairnea/KinDash#7",)  # unknown state fails closed
    assert rows["steiner385/viberpit#2"].live_session is True
    assert rows["steiner385/viberpit#4"].open_children == 1
    assert rows["steiner385/viberpit#5"].claimed is True
    assert rows["steiner385/viberpit#1"].product == "viberpit" and rows["steiner385/viberpit#1"].score == 8.0


def test_open_blocker_resolves_dst_across_products_and_closed_releases_it():
    """A dep's dst_ref may live in a different product's repo entirely (#999: dst
    resolution is unscoped across products). Seed a second product whose mirrored
    WorkItem is the dst of two deps from the first product: one to a CLOSED issue
    (releases the blocker) and one to an OPEN issue (still blocks)."""
    db = _db()
    _seed_product(db, "viberpit", "steiner385/viberpit", board=True)
    _seed_product(db, "kindash", "cairnea/KinDash", board=False)  # dst-only product, no board
    src_closed_dst = _item(db, "steiner385/viberpit", 10, status="Ready")
    src_open_dst = _item(db, "steiner385/viberpit", 11, status="Ready")
    _item(db, "cairnea/KinDash", 20, status="Ready", state="closed")  # closed blocker
    _item(db, "cairnea/KinDash", 21, status="Ready", state="open")  # open blocker
    db.add(WorkItemDep(src_ref="steiner385/viberpit#10", dst_ref="cairnea/KinDash#20", source="native",
                       active=True, src_work_item_id=src_closed_dst.id))
    db.add(WorkItemDep(src_ref="steiner385/viberpit#11", dst_ref="cairnea/KinDash#21", source="native",
                       active=True, src_work_item_id=src_open_dst.id))
    db.commit()

    rows = {r.ref: r for r in load_queue_rows(db, now=NOW)}

    assert rows["steiner385/viberpit#10"].open_blockers == ()  # closed dst releases the blocker
    assert rows["steiner385/viberpit#11"].open_blockers == ("cairnea/KinDash#21",)  # open dst still blocks


def test_parse_iso_utc_is_the_one_row_timestamp_parser():
    """The single ISO parser both the orchestrator and the runner read row stamps
    with. A stamp we cannot read is None — never a crash, never a cooldown."""
    at = datetime(2026, 9, 1, 12, 30, tzinfo=timezone.utc)
    assert parse_iso_utc(at.isoformat()) == at
    # A naive stamp (written before the runner stamped tz) is read as UTC.
    assert parse_iso_utc("2026-09-01T12:30:00") == at
    assert parse_iso_utc(None) is None
    assert parse_iso_utc("") is None
    assert parse_iso_utc("not-a-date") is None


def test_runner_automation_id_matches_the_registered_automation():
    """``queue_store`` holds the id as a literal (importing ``BacklogRunner`` there
    would be a cycle), so pin the two together — a renamed automation that silently
    matched no rows would empty the claim and give-up maps with no test failing."""
    from server.automations.backlog_runner import BacklogRunner
    assert queue_store.RUNNER_AUTOMATION_ID == BacklogRunner.id


def test_runner_row_maps_derives_claims_and_give_ups_from_the_runner_rows():
    """The ONE derivation of the backlog runner's durable rows into queue inputs —
    the runner's own pool read and ``GET /api/pm/queue`` both call it, so the
    operator's view and what agents do next can never disagree."""
    db = _db()
    db.add_all([Automation(id="backlog-runner"), Automation(id="pr-doctor")])
    at = datetime(2026, 9, 1, tzinfo=timezone.utc)

    def _state(target, state, automation_id="backlog-runner"):
        db.add(AutomationTargetState(automation_id=automation_id, target=target,
                                     state=state))

    _state("o/r#1", {"claimed": True, "issues": ["o/r#1", "o/r#2"]})   # bundle claim
    _state("o/r#3", {"claimed": True})                                # target IS the claim
    _state("o/r#4", {"claimed": False})                               # not a claim
    _state("o/r#5", {"gave_up": True, "issues": ["o/r#5", "o/r#6"],   # bundle give-up
                     "needs_human_at": at.isoformat()})
    _state("o/r#7", {"gave_up": True, "claimed": True,                # give-up wins over claim
                     "needs_human_at": at.isoformat()})
    _state("o/r#8", {"gave_up": True, "claimed": True})               # undated: neither map
    _state("o/r#9", {"claimed": True}, automation_id="pr-doctor")     # another automation
    db.commit()

    claimed, needs_human_at = runner_row_maps(db)

    assert claimed == frozenset({"o/r#1", "o/r#2", "o/r#3"})
    assert needs_human_at == {"o/r#5": at, "o/r#6": at, "o/r#7": at}


def test_load_queue_rows_threads_needs_human_at():
    """Phase D: the runner's durable give-up stamps ride onto the rows the pure
    core cools down from — the loader only threads the map, it never reads a label."""
    db = _db()
    _seed_product(db, "viberpit", "steiner385/viberpit", board=True)
    _item(db, "steiner385/viberpit", 5, status="Ready")
    _item(db, "steiner385/viberpit", 6, status="Ready")
    db.commit()
    at = datetime(2026, 9, 1, tzinfo=timezone.utc)

    rows = load_queue_rows(db, now=NOW,
                           needs_human_at={"steiner385/viberpit#5": at})

    by_ref = {r.ref: r for r in rows}
    assert by_ref["steiner385/viberpit#5"].needs_human_at == at
    assert all(r.needs_human_at is None for ref, r in by_ref.items()
               if ref != "steiner385/viberpit#5")


def test_load_queue_rows_computes_per_product_bounces_from_finalised_outcomes():
    """D8: only FINALISED (non-``running``/``merged``) outcomes at the CURRENT
    body fingerprint count. #1 has two ``no_pr`` rows at the current fingerprint,
    one ``merged`` row (excluded) and one ``no_pr`` row at a stale fingerprint
    (excluded, an edited body); #2 has no outcome rows at all."""
    db = _db()
    _seed_product(db, "viberpit", "steiner385/viberpit", board=True)
    _item(db, "steiner385/viberpit", 1, status="Ready")
    _item(db, "steiner385/viberpit", 2, status="Ready")
    fp = body_fingerprint(f"issue {1}", "body")
    stale_fp = body_fingerprint("an old title", "an old body")
    db.add_all([
        DispatchOutcome(target_ref="steiner385/viberpit#1", body_fingerprint=fp, outcome="no_pr"),
        DispatchOutcome(target_ref="steiner385/viberpit#1", body_fingerprint=fp, outcome="no_pr"),
        DispatchOutcome(target_ref="steiner385/viberpit#1", body_fingerprint=fp, outcome="merged"),
        DispatchOutcome(target_ref="steiner385/viberpit#1", body_fingerprint=stale_fp, outcome="no_pr"),
    ])
    db.commit()

    rows = {r.ref: r for r in load_queue_rows(db, now=NOW)}

    assert rows["steiner385/viberpit#1"].bounces == 2
    assert rows["steiner385/viberpit#2"].bounces == 0


def test_load_queue_rows_skips_products_without_a_board_and_closed_items():
    db = _db()
    _seed_product(db, "viberpit", "steiner385/viberpit", board=True)
    _seed_product(db, "kindash", "cairnea/KinDash", board=False)
    _item(db, "steiner385/viberpit", 1, status="Ready", score=5.0)
    _item(db, "steiner385/viberpit", 2, status="Ready", state="closed")
    _item(db, "cairnea/KinDash", 3, status="Ready", score=9.0)
    db.commit()

    rows = {r.ref: r for r in load_queue_rows(db, now=NOW)}

    assert set(rows) == {"steiner385/viberpit#1"}
