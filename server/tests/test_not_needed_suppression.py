"""Not-needed suppression (#2683): a ``mark_no_pr("not-needed")`` verdict keeps the
issue out of the queue until it MATERIALLY changes — a title/body edit, a label
change, a person's comment — with a backoff window for repeated verdicts on the
same unchanged material. Durable: it is read off the finalised outcome rows.

Regression cases are named after the issues the runner looped on:
cairnea/KinDash#17390 (~45 re-dispatches after a no-PR verdict), #17391 (~10),
#17030 (every 2–3 minutes) and steiner385/viberpit#2924 (re-claimed within the hour
after the no-PR marker).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from server.db.models import (NOT_NEEDED_CAUSE, Base, DispatchOutcome, GhProject,
                              GhProjectItem, Project, ProjectRepo, WorkItem)
from server.pm.outcomes import (NOT_NEEDED_BACKOFF, NotNeededVerdict, body_fingerprint,
                                material_fingerprint, not_needed_backoff,
                                not_needed_suppression)
from server.pm.policy_skip import NOT_NEEDED_HOLD_PREFIX, stale_system_holds
from server.pm.queue import SKIP_REASONS, QueueRow, rank, skip_reason
from server.pm.queue_store import load_queue_rows

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
TITLE, BODY, LABELS = "Fix the thing", "It is broken", ("bug",)
KW = dict(min_status="Backlog", respect_dependencies=True,
          trusted_authors=frozenset({"tony"}))


SESSION = timedelta(hours=2)   # how long each verdict's dispatch ran


def _verdict(at=T0, *, title=TITLE, body=BODY, labels=LABELS, legacy=False, started=True):
    return NotNeededVerdict(
        started_at=at - SESSION if started else None, ended_at=at,
        material_fingerprint=None if legacy else material_fingerprint(title, body, labels),
        body_fingerprint=body_fingerprint(title, body))


def _now(verdicts, **over):
    cur = dict(title=TITLE, body=BODY, labels=LABELS, last_human_comment_at=None,
               last_reopened_at=None)
    cur.update(over)
    return not_needed_suppression(verdicts, **cur)


def test_a_comment_made_while_the_session_ran_lifts_suppression():
    """#2683 review: the baseline is when the dispatch STARTED. A person who commented
    an hour into a two-hour session added something that session never read."""
    v = [_verdict()]
    assert _now(v, last_human_comment_at=T0 - timedelta(hours=1)) == (None, False)
    # Before the dispatch started: the session saw it.
    assert _now(v, last_human_comment_at=T0 - timedelta(hours=3))[1] is True
    # A row with no start time (a §13 recovery insert) falls back to its end.
    v = [_verdict(started=False)]
    assert _now(v, last_human_comment_at=T0 - timedelta(hours=1))[1] is True


def test_a_reopen_after_the_verdict_lifts_suppression():
    v = [_verdict()]
    assert _now(v, last_reopened_at=T0 + timedelta(days=2)) == (None, False)
    assert _now(v, last_reopened_at=T0 - timedelta(days=2))[1] is True


def test_verdicts_before_a_rearm_never_count_toward_the_backoff_rung():
    """Four verdicts, then a reopen, then one more: the window is the FIRST rung (a
    day), not the 30-day one the five together would reach."""
    old = [_verdict(T0 + timedelta(days=40 * n)) for n in range(4)]
    reopened = T0 + timedelta(days=200)
    latest = _verdict(reopened + timedelta(days=1))
    until, standing = _now([*old, latest], last_reopened_at=reopened)
    assert standing and until == reopened + timedelta(days=2)
    # The same with a person's comment as the re-arm.
    until, standing = _now([*old, latest], last_human_comment_at=reopened)
    assert standing and until == reopened + timedelta(days=2)


def test_material_fingerprint_ignores_label_order_and_case_but_not_content():
    a = material_fingerprint("t", "b", ["Bug", "ui"])
    assert a == material_fingerprint("t", "b", ["ui", "bug", " bug "])
    assert a != material_fingerprint("t", "b", ["ui"])
    assert a != material_fingerprint("t", "b2", ["Bug", "ui"])
    assert a != material_fingerprint("t2", "b", ["Bug", "ui"])


def test_backoff_never_comes_back_within_minutes_and_grows():
    assert [not_needed_backoff(n) for n in (1, 2, 3, 4, 9)] == [
        timedelta(days=1), timedelta(days=4), timedelta(days=16), timedelta(days=30),
        timedelta(days=30)]
    assert min(NOT_NEEDED_BACKOFF) >= timedelta(days=1)


def test_an_unchanged_issue_is_suppressed_for_the_window():
    until, standing = _now([_verdict()])
    assert standing and until == T0 + timedelta(days=1)


def test_kindash_17390_17391_repeat_verdicts_back_off():
    """Every further verdict on the same unchanged material widens the window."""
    verdicts = [_verdict(T0)]
    for n, gap in enumerate((timedelta(days=4), timedelta(days=16), timedelta(days=30)),
                            start=2):
        last = T0 + timedelta(days=n * 40)
        verdicts.append(_verdict(last))
        until, standing = _now(verdicts)
        assert standing and until == last + gap


def test_a_material_change_lifts_suppression_at_once():
    v = [_verdict()]
    assert _now(v, body="It is broken — repro: tap twice")[0] is None
    assert _now(v, title="Fix the thing on Android")[0] is None
    assert _now(v, labels=("bug", "priority:high"))[0] is None
    # A person commenting AFTER the verdict re-arms; one before it does not.
    assert _now(v, last_human_comment_at=T0 + timedelta(hours=2)) == (None, False)
    assert _now(v, last_human_comment_at=T0 - timedelta(hours=3))[1] is True


def test_a_verdict_from_before_material_fingerprints_falls_back_to_the_body():
    v = [_verdict(legacy=True)]
    assert _now(v)[1] is True
    assert _now(v, labels=("other",))[1] is True        # labels unknown for legacy rows
    assert _now(v, body="edited")[0] is None


def test_naive_verdict_times_read_as_utc():
    naive = NotNeededVerdict(started_at=None, ended_at=T0.replace(tzinfo=None),
                             material_fingerprint=material_fingerprint(TITLE, BODY, LABELS),
                             body_fingerprint=None)
    assert _now([naive])[0] == T0 + timedelta(days=1)


def _row(**over) -> QueueRow:
    base: dict[str, Any] = dict(ref="o/r#1", repo="o/r", number=1, product="p", title=TITLE, body=BODY,
                url="u", status="Ready", score=1.0, gh_created_at=T0, gh_updated_at=T0,
                assignees=(), author="tony", labels=LABELS, open_children=0,
                open_blockers=(), live_session=False, claimed=False)
    base.update(over)
    return QueueRow(**base)


def test_the_queue_skips_a_suppressed_issue_as_not_needed_until_the_window_ends():
    assert "not_needed" in SKIP_REASONS
    row = _row(not_needed_until=T0 + timedelta(days=1), not_needed_standing=True)
    assert skip_reason(row, now=T0 + timedelta(hours=23), **KW) == "not_needed"
    assert skip_reason(row, now=T0 + timedelta(days=1, seconds=1), **KW) is None


def test_kindash_17030_is_not_redispatched_every_few_minutes():
    """Re-dispatched every 2–3 minutes: after one verdict, no tick in the next day
    picks the unchanged issue."""
    until, _ = _now([_verdict()])
    row = _row(not_needed_until=until, not_needed_standing=True)
    for minutes in range(3, 24 * 60, 3):
        entries, skipped = rank([row], now=T0 + timedelta(minutes=minutes), **KW)
        assert entries == [] and skipped == {"not_needed": 1}


def test_runner_not_needed_holds_go_stale_while_the_verdict_stands():
    """The 15 KinDash holds reading "session found no PR is needed": the hygiene
    pass ends a RUNNER-written one while its verdict still stands (suppression keeps
    the issue out from then on), never a person's, never once the issue changed."""
    reason = f"{NOT_NEEDED_HOLD_PREFIX} (not-needed): ops tracking issue"
    held = dict(hold_kind="owner_action", hold_reason=reason)
    rows = [
        _row(number=1, ref="o/r#1", hold_by="runner", not_needed_standing=True, **held),
        _row(number=2, ref="o/r#2", hold_by="tony@example.com", not_needed_standing=True,
             **held),
        _row(number=3, ref="o/r#3", hold_by="runner", not_needed_standing=False, **held),
        # Another runner hand-off on a standing verdict is not this rule's to end.
        _row(number=4, ref="o/r#4", hold_by="runner", not_needed_standing=True,
             hold_kind="owner_action", hold_reason="bounced at fable: no mergeable PR"),
    ]
    assert [r.number for r in stale_system_holds(rows, {"p": {}}, now=T0)] == [1]


def _db():
    e = create_engine("sqlite://")
    Base.metadata.create_all(e)
    return Session(e)


def test_viberpit_2924_the_loader_suppresses_from_the_finalised_outcome_rows():
    """Durable across restarts: suppression is read off ``dispatch_outcomes`` and the
    mirror, nothing in memory. #2924 was re-claimed an hour after its no-PR marker."""
    db = _db()
    project = Project(slug="kindash", name="kindash")
    db.add(project)
    db.flush()
    db.add(ProjectRepo(project_id=project.id, repo="o/r"))
    gh = GhProject(project_id=project.id, gh_node_id="PVT_k", gh_project_number=1, title="k")
    db.add(gh)
    db.flush()
    wi = WorkItem(repo="o/r", number=1, kind="issue", state="open", title=TITLE, body=BODY,
                  labels=list(LABELS), author="tony", gh_created_at=T0, gh_updated_at=T0)
    db.add(wi)
    db.flush()
    db.add(GhProjectItem(gh_project_id=gh.id, work_item_id=wi.id, gh_item_node_id="PVTI_1",
                         status="Ready"))
    db.add(DispatchOutcome(automation_id="backlog-runner", target_ref="o/r#1",
                           outcome="no_pr", cause=NOT_NEEDED_CAUSE, ended_at=T0,
                           body_fingerprint=body_fingerprint(TITLE, BODY),
                           material_fingerprint=material_fingerprint(TITLE, BODY, LABELS)))
    db.commit()

    row, = load_queue_rows(db, now=T0 + timedelta(hours=1))
    assert row.not_needed_standing and row.not_needed_until == T0 + timedelta(days=1)
    assert skip_reason(row, now=T0 + timedelta(hours=1), **KW) == "not_needed"

    # A person comments after the verdict: eligible on the next read.
    wi.last_human_comment_at = T0 + timedelta(minutes=30)
    db.commit()
    row, = load_queue_rows(db, now=T0 + timedelta(hours=1))
    assert row.not_needed_until is None and not row.not_needed_standing
