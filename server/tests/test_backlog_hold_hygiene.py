"""Hold hygiene passes run after each backlog tick: one rolling issue per alert type
(#2680) and budgeted auto-approval of approval-gated escalations (#2679)."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from server.tests._trust_ids import TONY_ID, TONY_ENTRY
from server.db import db_session, init_db
from server.pm.queue import QueueRow
from server.tests.fakes.fake_automation_ctx import FakeAutomationContext

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
ESCALATION = "bounced at opus; fable needs approval"


def _row(**over) -> QueueRow:
    base: dict[str, Any] = dict(
        ref="o/r#1", repo="o/r", number=1, product="kindash", title="t", body="", url="u",
        status="Held", score=None, gh_created_at=NOW, gh_updated_at=NOW, assignees=(),
        author="tony", author_id=TONY_ID, labels=(), open_children=0, open_blockers=(), live_session=False,
        claimed=False)
    base.update(over)
    if "ref" not in over:
        base["ref"] = f"{base['repo']}#{base['number']}"
    return QueueRow(**base)


# --------------------------------------------------------------------------- #
# alert_series (#2680) — pure                                                  #
# --------------------------------------------------------------------------- #

def test_alert_key_needs_an_automation_author_and_a_date_suffix():
    from server.pm.alert_series import alert_key
    deploy = "⚠️ Unverified prod deploy 0531454bff — 2026-10-05"
    assert alert_key(deploy, "app/github-actions") == alert_key(
        "⚠️ Unverified prod deploy 035f72c08c — 2026-10-04", "github-actions[bot]")
    assert alert_key("Weekly failed — 2026-10-04", "app/github-actions") == "weekly failed"
    # Counts are per-occurrence too.
    assert alert_key("Site Crawler: 3 errors found — 2026-10-04", "app/github-actions") == \
        alert_key("Site Crawler: 12 errors found — 2026-10-01", "app/github-actions")
    # A person's dated title is not a series; neither is an undated bot title.
    assert alert_key("Sprint review — 2026-10-01", "tony") is None
    assert alert_key("Dependency Dashboard", "renovate[bot]") is None
    # An all-letter word is not a sha.
    assert alert_key("defaced page — 2026-10-01", "app/github-actions") == "defaced page"


def test_superseded_keeps_the_newest_per_repo_and_type():
    from server.pm.alert_series import AlertIssue, superseded
    gha = "app/github-actions"
    issues = [
        AlertIssue("o/k", 10, "⚠️ Unverified prod deploy 1111111aaa — 2026-09-20", gha),
        AlertIssue("o/k", 30, "⚠️ Unverified prod deploy 3333333ccc — 2026-10-05", gha),
        AlertIssue("o/k", 20, "⚠️ Unverified prod deploy 2222222bbb — 2026-10-01", gha),
        AlertIssue("o/k", 15, "Weekly failed — 2026-09-27", gha),
        AlertIssue("o/k", 25, "Weekly failed — 2026-10-04", gha),
        AlertIssue("o/v", 5, "Weekly failed — 2026-09-27", gha),     # other repo: alone
        AlertIssue("o/k", 40, "Fix the weekly job", "tony"),
    ]
    assert superseded(issues) == [("o/k#10", "o/k#30"), ("o/k#15", "o/k#25"),
                                  ("o/k#20", "o/k#30")]


# --------------------------------------------------------------------------- #
# auto_escalation (#2679) — pure                                               #
# --------------------------------------------------------------------------- #

# A product that auto-escalates its flaky issues works its trackers (#2683): with the
# default ``skip_self_closing_trackers`` the queue skips a ``flaky`` issue as
# ``tracker``, and the approval tap refuses to dispatch what the queue skips.
POLICY = {"dispatch": "auto", "trusted_authors": [TONY_ENTRY],
          "auto_escalate_match": ["flaky", "Unverified prod"],
          "auto_escalate_budget": 2, "skip_self_closing_trackers": False}


def _escalated(number, *, title="t", labels=(), by="runner", set_days_ago=1, **over):
    return _row(number=number, title=title, labels=labels, hold_kind="owner_action",
                hold_reason=ESCALATION, hold_by=by,
                hold_set_at=(NOW - timedelta(days=set_days_ago)).replace(tzinfo=None),
                work_item_id=f"wi{number}", **over)


def test_auto_escalation_candidates_match_label_or_title_and_respect_the_budget():
    from server.pm.auto_escalation import candidates, matches
    assert matches(POLICY, title="[flaky] x", labels=()) == "flaky"
    assert matches(POLICY, title="x", labels=("FLAKY",)) == "flaky"
    assert matches(POLICY, title="⚠️ Unverified prod deploy a — d", labels=()) == "Unverified prod"
    assert matches(POLICY, title="x", labels=("bug",)) is None
    rows = [
        _escalated(1, labels=("flaky",), set_days_ago=1),
        _escalated(2, title="[flaky] older", set_days_ago=5),
        _escalated(3, title="[flaky] oldest", set_days_ago=9),
        _escalated(4, title="not covered"),
        _escalated(5, title="[flaky] a person's", by="tony@example.com"),
        _row(number=6, title="[flaky] plain hold", hold_kind="owner_action",
             hold_reason="PR closed unmerged", hold_by="runner"),
        _escalated(7, title="[flaky] other product", product="viberpit"),
    ]
    picked = candidates(rows, {"kindash": POLICY}, {"kindash": 1})
    # Budget 2, one spent: the OLDEST covered hold goes first, and only one.
    assert [(r.ref, esc.needs_approval, entry) for r, esc, entry in picked] == [
        ("o/r#3", "fable", "flaky")]
    assert candidates(rows, {"kindash": {**POLICY, "auto_escalate_budget": 0}}, {}) == []


# --------------------------------------------------------------------------- #
# Orchestrator passes                                                          #
# --------------------------------------------------------------------------- #

@pytest.fixture
def db_env():
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite+aiosqlite:///{tmp}/t.db"
        with patch.dict(os.environ, {"VIBERPIT_DB_URL": url, "VIBERPIT_DEV": "1",
                                     "VIBERPIT_TAILNET_ALLOWLIST": ""}):
            yield url


def _orch(rows, policies):
    from server.sessions.orchestrator import SessionOrchestrator
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._backlog_ctx = FakeAutomationContext()

    async def queue_inputs():
        return rows, policies

    orch._backlog_queue_inputs = queue_inputs
    return orch


@pytest.mark.asyncio
async def test_supersede_alerts_closes_older_alerts_but_never_a_claimed_one(db_env):
    from sqlalchemy import select

    from server.db.models import WorkItem
    await init_db(db_env)
    gha = "app/github-actions"
    rows = [_row(number=n, title=f"Weekly failed — 2026-09-{n:02d}", author=gha,
                 claimed=(n == 11)) for n in (10, 11, 12, 13)]
    rows.append(_row(repo="o/off", number=1, title="Weekly failed — 2026-09-01", author=gha,
                     product="off"))
    rows.append(_row(repo="o/off", number=2, title="Weekly failed — 2026-09-02", author=gha,
                     product="off"))
    async with db_session() as db:
        for r in rows:
            db.add(WorkItem(repo=r.repo, number=r.number, kind="issue", state="open",
                            title=r.title, body=""))
    orch = _orch(rows, {"kindash": {"dispatch": "auto"}, "off": {"dispatch": "off"}})
    calls: list[list[str]] = []

    def gh_run(args, token=None):
        calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    orch._gh_run = gh_run
    with patch("viberpit.gitrepos.app_token_for_repo", lambda repo: None):
        closed = await orch._backlog_supersede_alerts()

    # #11 is claimed (a live session is on it); #13 is the newest; dispatch-off
    # products are not this loop's to touch.
    assert closed == ["o/r#10", "o/r#12"]
    assert [c[2] for c in calls] == ["10", "12"]
    assert all("Superseded by #13" in c[c.index("--comment") + 1] for c in calls)
    async with db_session() as db:
        states = dict((await db.execute(select(WorkItem.number, WorkItem.state).where(
            WorkItem.repo == "o/r"))).all())
    assert states == {10: "closed", 11: "open", 12: "closed", 13: "open"}
    assert [kw["kind"] for _, _, kw in orch._backlog_ctx.events] == ["alert_superseded"] * 2


async def _seed_escalations(numbers, *, labels=("flaky",)):
    from sqlalchemy import select

    from server.db.models import Project, ProjectRepo, WorkItem
    async with db_session() as db:
        # The approval tap re-reads the product's policy, which is fail-closed on authors.
        if (await db.execute(select(Project).where(Project.slug == "kindash"))).first() is None:
            proj = Project(slug="kindash", name="kindash", automation_policy=POLICY)
            db.add(proj)
            await db.flush()
            db.add(ProjectRepo(project_id=proj.id, repo="o/r", is_primary=True))
        for n in numbers:
            db.add(WorkItem(id=f"wi{n}", repo="o/r", number=n, kind="issue", state="open",
                            title="t", body="", labels=list(labels), author="tony", author_id=TONY_ID,
                            hold_kind="owner_action", hold_reason=ESCALATION, hold_by="runner",
                            hold_set_at=(NOW - timedelta(days=n)).replace(tzinfo=None)))


@pytest.mark.asyncio
async def test_auto_approve_escalations_dispatches_within_budget_and_records_it(db_env):
    from sqlalchemy import select

    from server.db.models import AuditEvent, WorkItem
    from server.pm.auto_escalation import AUTO_APPROVED_AUDIT
    await init_db(db_env)
    await _seed_escalations([1, 2])
    rows = [_escalated(n, labels=("flaky",), set_days_ago=n) for n in (1, 2)]
    orch = _orch(rows, {"kindash": {**POLICY, "auto_escalate_budget": 1}})
    dispatched: list = []
    approved: list = []

    async def dispatch(refs, model=None):
        dispatched.append((refs, model))
        return {"status": "spawned", "session_id": f"s-{refs[0]}", "session_name": "n"}

    async def resolve(session_id, decision, *, identity, model=None):
        approved.append((session_id, decision, identity))

    orch.backlog_runner_dispatch = dispatch
    orch.resolve_model_approval = resolve

    assert await orch._backlog_auto_approve_escalations() == ["o/r#2"]   # oldest first
    assert dispatched == [(["o/r#2"], "fable")]
    assert approved == [("s-o/r#2", "approve", "backlog-runner")]
    async with db_session() as db:
        holds = dict((await db.execute(select(WorkItem.number, WorkItem.hold_kind))).all())
        audit = (await db.execute(select(AuditEvent).where(
            AuditEvent.type == AUTO_APPROVED_AUDIT))).scalars().all()
        release = (await db.execute(select(AuditEvent).where(
            AuditEvent.type == "pm.hold.release"))).scalars().one()
    assert holds == {1: "owner_action", 2: None}
    assert [(a.identity, a.detail["product"], a.detail["matched"], a.detail["model"])
            for a in audit] == [("backlog-runner", "kindash", "flaky", "fable")]
    assert release.detail["signal"] == "escalation" and release.via == "runner"
    (level, summary, kw), = orch._backlog_ctx.events
    assert kw["kind"] == "auto_escalated" and "1/1 this week" in summary

    # The budget is spent for the week: the other hold waits for a person.
    rows.pop()
    assert await orch._backlog_auto_approve_escalations() == []
    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_needs_human_skips_the_comment_for_a_covered_escalation(db_env):
    from server.automations.context import AutomationContext
    from server.db.models import Automation
    await init_db(db_env)
    await _seed_escalations([3])
    async with db_session() as db:
        db.add(Automation(id="backlog-runner"))
    orch = _orch([_escalated(3, labels=("flaky",))], {"kindash": POLICY})
    orch._backlog_ctx = AutomationContext("backlog-runner")
    orch._gh_run = object()
    await orch._backlog_ctx.set_target_state("o/r#3", {"repo": "o/r", "issues": ["o/r#3"]})
    announced: list = []

    def announce(batch, reason, *, gh_run):
        announced.append(reason)

    with patch("server.automations.backlog_github.announce_needs_human", announce):
        await orch._backlog_needs_human("o/r#3", ESCALATION)
        assert announced == []
        # Not covered (budget off): the owner is told, as before.
        orch._backlog_queue_inputs = _orch([_escalated(3, labels=("flaky",))],
                                           {"kindash": {**POLICY, "auto_escalate_budget": 0}}
                                           )._backlog_queue_inputs
        await orch._backlog_needs_human("o/r#3", ESCALATION)
        assert announced == [ESCALATION]


@pytest.mark.asyncio
async def test_policy_skipped_runner_holds_are_released_so_nothing_is_left_to_approve(db_env):
    """cairnea/KinDash#16652: the runner handed the issue off ("bounced at opus;
    fable needs approval") on 09-23; the product added ``forced-deploy-audit`` to
    ``skip_labels`` on 10-03; the stale hand-off stayed in the Held inbox and its
    approve tap on 10-06 dispatched an issue the policy says is not work. The pass
    ends every hand-off the runner wrote on an issue the policy now skips — a
    person's own park, and holds of other kinds, are left alone."""
    from sqlalchemy import select

    from server.db.models import AuditEvent, WorkItem
    await init_db(db_env)
    audit = ("ci-infrastructure", "forced-deploy-audit")
    await _seed_escalations([1, 2], labels=audit)
    await _seed_escalations([3], labels=("bug",))
    async with db_session() as db:
        db.add(WorkItem(id="wi4", repo="o/r", number=4, kind="issue", state="open", title="t",
                        body="", labels=list(audit), hold_kind="owner_action",
                        hold_reason="keep", hold_by="tony@example.com",
                        hold_set_at=NOW.replace(tzinfo=None)))
    rows = [_escalated(n, labels=audit, set_days_ago=n) for n in (1, 2)]
    rows.append(_escalated(3, labels=("bug",)))
    rows.append(_row(number=4, hold_kind="owner_action", hold_by="tony@example.com",
                     hold_reason="keep", labels=audit, work_item_id="wi4"))
    orch = _orch(rows, {"kindash": {**POLICY, "skip_labels": ["forced-deploy-audit"]}})

    assert await orch._backlog_release_policy_skipped_holds() == ["o/r#1", "o/r#2"]

    async with db_session() as db:
        holds = dict((await db.execute(select(WorkItem.number, WorkItem.hold_kind))).all())
        releases = (await db.execute(select(AuditEvent).where(
            AuditEvent.type == "pm.hold.release"))).scalars().all()
    assert holds == {1: None, 2: None, 3: "owner_action", 4: "owner_action"}
    assert [(r.detail["signal"], r.detail["kind"], r.via) for r in releases] == [
        ("policy_skip", "owner_action", "runner")] * 2
    assert [(kw["kind"], kw["target"]) for _, _, kw in orch._backlog_ctx.events] == [
        ("policy_skip_released", "o/r#1"), ("policy_skip_released", "o/r#2")]

    # Converges: the released rows are unheld now, so a second pass finds nothing.
    rows[:2] = [_row(number=n, labels=audit, work_item_id=f"wi{n}") for n in (1, 2)]
    assert await orch._backlog_release_policy_skipped_holds() == []


@pytest.mark.asyncio
async def test_policy_skipped_holds_pass_logs_and_returns_nothing_when_the_read_fails(db_env):
    await init_db(db_env)
    orch = _orch([], {})

    async def boom():
        raise RuntimeError("db down")

    orch._backlog_queue_inputs = boom
    assert await orch._backlog_release_policy_skipped_holds() == []
    assert orch._backlog_ctx.events == []
