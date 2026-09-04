"""Golden convergence test for the 003 reconciler — real modules, real DB, no canned facts.

Every other reconciler test fakes one seam. This one wires the actual pipeline over an
in-memory SQLite database and runs it for several cycles:

    seed → load_product_facts → derive/diff_fields/membership → journal → "GitHub"
    (the pushed values echoed back through mirror_store with a NEWER updated_at) → reload

and asserts the two properties that no single-module test can:

1. **It converges.** After the writes land and are re-ingested, the next cycle plans
   nothing — the echo is recognized as ours (no ``HumanEdit``) and the diff is empty.
2. **Our own writes don't stick.** Our write stamps the item's ``gh_updated_at`` with OUR
   time, which out-ranks every older decision. When the session ends and Status falls back
   to ``Ready`` (carrying the much older ``classified_at``), the push must still happen —
   ownership belongs to humans, not to our own past writes.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from server.db.models import (Base, GhProject, GhProjectItem, Project, ProjectRepo,
                              Session as DbSession, WorkItem)
from server.pm import journal, mirror_store
from server.pm.desired_state import derive
from server.pm.gh_projects import GhProjectItemView
from server.pm.mirror_store import list_product_views
from server.pm.reconcile_diff import diff_fields, membership
from server.pm.reconcile_inputs import load_product_facts

REPO = "steiner385/cc-account-switcher"
T = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def _db():
    e = create_engine("sqlite://"); Base.metadata.create_all(e); return Session(e)


def _seed(db):
    """One classified issue with a live session and a mirror row, plus one open issue
    that is not on the board yet (so membership has something to do)."""
    p = Project(slug="ccswitch", name="ccswitch"); db.add(p); db.flush()
    db.add(ProjectRepo(project_id=p.id, repo=REPO))
    gp = GhProject(project_id=p.id, gh_node_id="PVT_1", gh_project_number=26, title="ccswitch",
                   field_cache={}, last_synced_at=T)
    db.add(gp); db.flush()
    i5 = WorkItem(repo=REPO, number=5, kind="issue", state="open", gh_node_id="I_5",
                  triage_state="classified", classified_at=T, reach=4, impact=3, effort=2,
                  confidence=80, labels=[])
    i9 = WorkItem(repo=REPO, number=9, kind="issue", state="open", gh_node_id="I_9", labels=[])
    db.add_all([i5, i9]); db.flush()
    db.add(GhProjectItem(gh_project_id=gp.id, work_item_id=i5.id, gh_item_node_id="PVTI_5",
                         status="Backlog", gh_updated_at=T - timedelta(days=1)))
    db.add(DbSession(name="s", account_name="a", cwd="/tmp", repo=REPO, origin="backlog-runner",
                     origin_ref=f"{REPO}#5", status="in_progress", updated_at=T))
    db.commit()
    return gp.id


def _plan(db, product, now):
    """One cycle's plan, exactly as GhReconcileLoop._converge composes it."""
    facts = load_product_facts(db, product, now=now)
    own_values = journal.own_applied_values(db)
    muts = []
    for item in facts.items:
        mirror = facts.mirror.get(item.ref)
        if mirror is not None:
            muts.extend(diff_fields(derive(item), mirror, own_values))
    muts.extend(membership(facts.open_issue_nodes, set(facts.mirror), facts.project_node_id))
    return facts, muts


def _apply(db, muts, now):
    journal.mark_applied(db, journal.record_pending(
        db, muts, batch_id=f"b-{now:%H%M%S}", now=now, dry_run=False), now)
    db.flush()


def _github_echo(db, gid, facts, muts, now):
    """Simulate GitHub: apply the mutations we just sent to the board's state, stamp a
    NEWER ``updated_at`` on every touched item (GitHub always does), and feed the result
    back through the real read path with the real echo-suppression key set."""
    state = {m.item_node_id: {"ref": ref, "status": m.status, "score": m.score, "stage": m.stage}
             for ref, m in facts.mirror.items()}
    node_to_ref = {node: ref for ref, node in facts.open_issue_nodes.items()}
    for mut in muts:
        if mut.kind == "set_field":
            state[mut.target_node_id][(mut.field or "").lower()] = mut.new_value
        elif mut.kind == "add_item":
            ref = node_to_ref[str(mut.new_value)]
            state[f"PVTI_{ref.rsplit('#', 1)[1]}"] = {
                "ref": ref, "status": None, "score": None, "stage": None}
    views = []
    for node, s in state.items():
        repo, _, number = str(s["ref"]).rpartition("#")
        views.append(GhProjectItemView(item_node_id=node, repo=repo, number=int(number),
                                       kind="issue", status=s["status"], score=s["score"],
                                       stage=s["stage"], updated_at=now.isoformat()))
    own = journal.recent_own_writes(db, now - timedelta(hours=24))
    edits = mirror_store.upsert_project_items(db, gid, views, now, own_writes=own)
    db.flush()
    return edits


def _cycle(db, product, gid, now):
    facts, muts = _plan(db, product, now)
    if muts:
        _apply(db, muts, now)
    edits = _github_echo(db, gid, facts, muts, now + timedelta(minutes=1))
    return muts, edits


def test_reconciler_converges_and_own_writes_never_stick():
    db = _db()
    gid = _seed(db)
    product = list_product_views(db)[0]

    # Cycle 1: the live session drives Status, the RICE classification drives Score, and
    # the un-mirrored open issue is added to the board.
    muts, edits = _cycle(db, product, gid, T)
    assert sorted((m.kind, m.field, m.new_value) for m in muts) == [
        ("add_item", None, "I_9"),
        ("set_field", "Score", 480.0),
        ("set_field", "Status", "In progress")]
    assert edits == []          # our own writes echoed back are NOT human edits

    # Cycle 2: #5 is converged (this is the assertion that fails if our own write is read
    # back as a human edit). #9 is on the board now, so membership is done too; its Status
    # is still empty, so it gets its initial fill — the "second apply" the runbook calls out.
    muts, edits = _cycle(db, product, gid, T + timedelta(minutes=2))
    assert [(m.target_node_id, m.field, m.new_value) for m in muts] == [
        ("PVTI_9", "Status", "Backlog")]
    assert edits == []

    # Cycle 3: fully converged — nothing left to push.
    muts, edits = _cycle(db, product, gid, T + timedelta(minutes=4))
    assert muts == [] and edits == []

    # The session ends. Status falls back to Ready carrying the ORIGINAL classified_at,
    # which is older than the gh_updated_at our own "In progress" write produced. Without
    # own-write un-sticking this is stuck on "In progress" forever.
    session = db.query(DbSession).one()
    session.status = "complete"
    session.updated_at = T + timedelta(minutes=5)
    db.commit()
    muts, edits = _cycle(db, product, gid, T + timedelta(minutes=6))
    assert [(m.target_node_id, m.field, m.old_value, m.new_value) for m in muts] == [
        ("PVTI_5", "Status", "In progress", "Ready")]
    assert edits == []

    # ...and that push converges too.
    muts, _ = _cycle(db, product, gid, T + timedelta(minutes=8))
    assert muts == []


def test_a_real_human_edit_is_detected_and_wins():
    """The mirror image of the test above: if the value on the board is NOT what we last
    wrote, it's a human decision — it surfaces as a HumanEdit and outranks our older one."""
    db = _db()
    gid = _seed(db)
    product = list_product_views(db)[0]
    _cycle(db, product, gid, T)                       # we push "In progress"

    facts = load_product_facts(db, product, now=T)
    human_time = T + timedelta(minutes=3)
    views = [GhProjectItemView(item_node_id="PVTI_5", repo=REPO, number=5, kind="issue",
                               status="Blocked", score=480.0, stage=None,
                               updated_at=human_time.isoformat()),
             GhProjectItemView(item_node_id="PVTI_9", repo=REPO, number=9, kind="issue",
                               status=None, score=None, stage=None,
                               updated_at=human_time.isoformat())]
    edits = mirror_store.upsert_project_items(
        db, gid, views, human_time,
        own_writes=journal.recent_own_writes(db, T - timedelta(hours=24)))
    db.flush()
    assert [(e.ref, e.field, e.old, e.new) for e in edits] == [
        (f"{REPO}#5", "Status", "In progress", "Blocked")]

    # The session is still live, so we'd like "In progress" back — but the human's edit is
    # newer than our decision, so the ownership rule suppresses it.
    _facts, muts = _plan(db, product, human_time)
    assert [(m.target_node_id, m.field) for m in muts] == [("PVTI_9", "Status")]
