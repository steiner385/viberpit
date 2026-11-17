"""#3402: KinDash ``ci:regressed`` issues dispatch as P0 — ahead of ordinary
backlog, even when the tracker also carries ``flaky``, bundled with their CI
Failure Reporter twin, and surfaced to the owner once unowned for a day."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from server.automations.backlog_grouping import (family_groups, plan_to_batch,
                                                 regressed_groups)
from server.automations.backlog_prompt import build_implement_prompt
from server.automations.backlog_selection import Batch
from server.pm.queue import (QueueRow, ci_twin_key, is_urgent, rank, regressed_unowned,
                             skip_reason, to_candidate)
from server.pm.tree import issue_reason

NOW = datetime(2026, 11, 8, 9, 30, tzinfo=timezone.utc)
REPO = "cairnea/KinDash"
TEST = ("dashboard/dashboard/family-hub-desk.spec.ts › Family Hub — desk presentation › "
        "Customize opens the Family Hub settings page on its own, at the top")
BOT = "app/github-actions"


def _row(number: int, title: str, labels: tuple[str, ...], **over) -> QueueRow:
    base: dict[str, Any] = dict(ref=f"{REPO}#{number}", repo=REPO, number=number, product="kindash",
                title=title, body="", url=f"https://github.com/{REPO}/issues/{number}",
                status=None, score=None, gh_created_at=NOW - timedelta(minutes=5),
                gh_updated_at=NOW, assignees=(), author=BOT, labels=labels, open_children=0,
                open_blockers=(), live_session=False, claimed=False)
    base.update(over)
    return QueueRow(**base)


def _tracker(**over) -> QueueRow:
    return _row(21010, f"[flaky] {TEST} (fp:1a958cf3)", ("flaky", "ci:regressed"), **over)


def _twin(**over) -> QueueRow:
    return _row(21009, f"[playwright] {TEST}", ("ci-failure", "tool:playwright"), **over)


def _escalation() -> QueueRow:
    return _row(21008, "🚨 10 of last 10 nightly runs failed — prod promotion blocked",
                ("bug", "ci-infrastructure", "nightly-consecutive-failure"))


def _backlog(number: int, score: float) -> QueueRow:
    return _row(number, f"Feature {number}", (), score=score, status="Ready",
                author="tony", author_id=1, gh_created_at=NOW - timedelta(days=30))


def _rank(rows, **kw):
    kw.setdefault("min_status", "Ready")   # the strictest gate a product can pick
    return rank(rows, now=NOW, trusted_authors=frozenset({"user:1"}), **kw)


# --- the queue -------------------------------------------------------------------------------

def test_the_twins_share_a_key_and_unrelated_issues_do_not():
    tracker, twin = _tracker(), _twin()
    key = ci_twin_key(tracker.repo, tracker.labels, tracker.title)
    assert key is not None and key == ci_twin_key(twin.repo, twin.labels, twin.title)
    assert ci_twin_key(REPO, ("ci-failure",), "[playwright] other.spec.ts › x") != key
    assert ci_twin_key("other/repo", twin.labels, twin.title) != key
    assert ci_twin_key(REPO, ("bug",), twin.title) is None   # neither label: no key


def test_a_flaky_regressed_tracker_is_urgent_work_not_a_skipped_tracker():
    kw = dict(min_status="Ready", respect_dependencies=True, now=NOW)
    assert skip_reason(_tracker(), **kw) is None
    # The same tracker without ``ci:regressed`` is still a self-closing tracker.
    plain = _row(21010, f"[flaky] {TEST} (fp:1a958cf3)", ("flaky",))
    assert skip_reason(plain, **{**kw, "min_status": "Backlog"}) == "tracker"
    # Unscored and un-groomed, it still passes a Ready-mode gate — but not Done.
    assert skip_reason(_tracker(status="Backlog"), **kw) is None
    assert skip_reason(_tracker(status="Done"), **kw) == "status"
    assert is_urgent(_tracker())


def test_a_regressed_tracker_ranks_ahead_of_scored_backlog():
    entries, _ = _rank([_backlog(1, 99.0), _tracker()])
    assert [e.number for e in entries] == [21010, 1]
    assert entries[0].urgent and entries[0].regressed


def test_a_twin_whose_tracker_is_already_being_worked_waits_for_that_session():
    for owned in (dict(claimed=True), dict(live_session=True)):
        skipped: list = []
        entries, counts = _rank([_tracker(**owned), _twin()], skipped_out=skipped,
                                min_status="Backlog")
        assert entries == []
        assert ("claimed" if "claimed" in owned else "live_session") in counts
        assert (f"{REPO}#21009", "claimed") in [(r.ref, why) for r, why in skipped]
    # Without a regressed twin a CI Failure Reporter issue is ordinary work, gated
    # like any other un-groomed issue.
    assert _rank([_twin()])[0] == []
    entries, _ = _rank([_twin()], min_status="Backlog")
    assert [e.number for e in entries] == [21009]
    # A Done twin is not rescued by its tracker.
    entries, _ = _rank([_tracker(), _twin(status="Done")])
    assert [e.number for e in entries] == [21010]
    # Nor is a twin that fails any OTHER gate: the grooming waiver is the only one.
    for bad, reason in ((dict(author="mallory", author_id=99), "untrusted"),
                        (dict(assignees=("someone",)), "assigned"),
                        (dict(hold_kind="owner_action"), "held")):
        skipped = []
        entries, _ = _rank([_tracker(), _twin(**bad)], skipped_out=skipped)
        assert [e.number for e in entries] == [21010]
        assert (f"{REPO}#21009", reason) in [(r.ref, why) for r, why in skipped]


# --- grouping and selection -------------------------------------------------------------------

def _candidates(rows, **kw):
    entries, _ = _rank(rows, **kw)
    return [to_candidate(e) for e in entries]


def test_replaying_11_08_dispatches_exactly_one_session_on_the_next_tick():
    # The tracker carries ``flaky`` + ``ci:regressed``, the escalation is open, and
    # the stored plan predates the label (it holds only ordinary backlog).
    pool = _candidates([_tracker(), _twin(), _escalation(), _backlog(1, 99.0)])
    assert f"{REPO}#21008" not in {c["ref"] for c in pool}   # the escalation self-closes
    stale_plan = [{"id": 7, "refs": [f"{REPO}#1"]}]
    batch, group_id = plan_to_batch(stale_plan, pool)
    assert batch is not None and group_id is None   # synthesized, not a plan group
    assert batch.primary == f"{REPO}#21010"
    assert batch.issues == [f"{REPO}#21010", f"{REPO}#21009"]
    # Once claimed, neither twin is offered again: one session, not two.
    pool = _candidates([_tracker(claimed=True), _twin(), _backlog(1, 99.0)])
    batch, group_id = plan_to_batch(stale_plan, pool)
    assert batch is not None and batch.issues == [f"{REPO}#1"] and group_id == 7


def test_a_planned_twin_is_pulled_into_the_tracker_session():
    pool = _candidates([_tracker(), _twin(), _backlog(1, 99.0)])
    plan = [{"id": 3, "refs": [f"{REPO}#1", f"{REPO}#21009"]}]
    batch, _ = plan_to_batch(plan, pool)
    assert batch.issues == [f"{REPO}#21010", f"{REPO}#21009"]


def test_a_regressed_group_leads_whatever_the_repo_strategy_prefers():
    other = _row(5, "Other feature", (), ref="o/r#5", repo="o/r", score=50.0,
                 status="Ready", author="tony", author_id=1)
    pool = _candidates([_tracker(), other])
    plan = [{"id": 1, "refs": ["o/r#5"]}, {"id": 2, "refs": [f"{REPO}#21010"]}]
    batch, _ = plan_to_batch(plan, pool, repo_load={REPO: 3, "o/r": 0})
    assert batch.primary == f"{REPO}#21010"


def test_family_groups_plans_the_twins_together_and_keeps_them_from_the_model():
    pool = _candidates([_tracker(), _twin(), _backlog(1, 99.0)])
    groups, rest = family_groups(pool, max_k=1)
    assert groups[0]["refs"] == [f"{REPO}#21010", f"{REPO}#21009"]
    assert [c["ref"] for c in rest] == [f"{REPO}#1"]
    lone, _ = regressed_groups(_candidates([_tracker()]))
    assert lone == [{"refs": [f"{REPO}#21010"],
                     "rationale": "ci:regressed: a deterministic CI break, dispatched first"}]


# --- the session's prompt ----------------------------------------------------------------------

def test_the_prompt_refs_the_tracker_and_fixes_the_twin():
    batch = Batch(primary=f"{REPO}#21010", issues=[f"{REPO}#21010", f"{REPO}#21009"],
                  repo=REPO, trackers=(f"{REPO}#21010",))
    prompt = build_implement_prompt(batch, "main")
    assert "`Refs #21010 (left-open: awaiting_deploy_or_condition)`" in prompt
    assert "never `Fixes`" in prompt
    assert "close with `Fixes #21009`" in prompt
    assert "close with `Fixes #21010`" not in prompt


# --- needs-you ---------------------------------------------------------------------------------

def test_an_unowned_regressed_issue_needs_you_after_a_day():
    labels = ("flaky", "ci:regressed")
    old = NOW - timedelta(hours=25)
    assert regressed_unowned(labels=labels, created_at=old, owned=False, now=NOW)
    assert not regressed_unowned(labels=labels, created_at=old, owned=True, now=NOW)
    assert not regressed_unowned(labels=labels, created_at=NOW - timedelta(hours=23),
                                 owned=False, now=NOW)
    assert not regressed_unowned(labels=("flaky",), created_at=old, owned=False, now=NOW)
    assert not regressed_unowned(labels=labels, created_at=None, owned=False, now=NOW)
    assert issue_reason(state="open", regressed_unowned=True) == "regressed_unowned"
    assert issue_reason(state="closed", regressed_unowned=True) is None
    assert issue_reason(state="open", bounces=2, give_up_after_bounces=2,
                        regressed_unowned=True) == "needs_spec"


def test_roadmap_needs_you_lists_a_homeless_regressed_issue_unowned_past_a_day(isolated_db):
    import asyncio

    from server.api.pm_tree import regressed_homeless
    from server.db import db_session
    from server.db.models import AutomationTargetState, WorkItem
    from server.pm.queue_store import RUNNER_AUTOMATION_ID

    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=30)
    fresh = old + timedelta(hours=29)

    def wi(wid, number, labels, *, created=old, state="open", repo=REPO):
        return WorkItem(id=wid, repo=repo, number=number, kind="issue", state=state,
                        labels=list(labels), gh_created_at=created, title=f"t{number}")

    async def run():
        async with db_session() as db:
            db.add_all([
                wi("a", 1, ("flaky", "ci:regressed")),
                wi("b", 2, ("ci:regressed",), created=fresh),
                wi("c", 3, ("flaky",)),
                wi("d", 4, ("ci:regressed",), state="closed"),
                wi("e", 5, ("ci:regressed",), repo="o/other"),
                wi("f", 6, ("ci:regressed",)),
                AutomationTargetState(automation_id=RUNNER_AUTOMATION_ID,
                                      target=f"{REPO}#6", state={"claimed": True}),
            ])
        async with db_session() as db:
            return [w.id for w in await regressed_homeless(db, [REPO])]

    assert asyncio.run(run()) == ["a"]
