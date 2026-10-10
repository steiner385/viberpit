"""PR effect exclusion across aliases, sources and overlapping sets using SQL."""
import asyncio
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from server.automations.context import AutomationContext
from server.automations.ownership import CanonicalTarget, owned
from server.db.models import Automation, AutomationOperation
from server.github.pr_status import PullRequestInfo
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.test_automation_ownership import ownership_db as ownership_db

# PR Doctor refuses untrusted/unmirrored PRs; these tests cover its other behaviour.
pytestmark = pytest.mark.usefixtures("pr_authors_trusted")


@pytest_asyncio.fixture
async def doctor(ownership_db, monkeypatch):
    async with ownership_db() as db:
        db.add(Automation(id="pr-doctor"))
        await db.commit()

    async def resolve(refs, *, kind, include_linked_issues=False):
        assert kind == "pr" and include_linked_issues
        return tuple(CanonicalTarget("PR_" + ref.split("#")[1], ref) for ref in refs)

    monkeypatch.setattr("server.automations.target_identity.resolve_targets", resolve)
    monkeypatch.setattr("server.automations.ownership_reconciliation.reconcile_legacy_targets", AsyncMock())
    orch = object.__new__(SessionOrchestrator)
    orch._pr_doctor_ctx = AutomationContext("pr-doctor")
    orch._pr_doctor_ctx.emit = AsyncMock()
    orch._automation_session_work_stopped = AsyncMock(return_value=False)
    return orch


def info(n=1):
    return PullRequestInfo(number=n, url=f"https://github.com/o/r/pull/{n}", state="open",
                           checks="failing", title="fix", head_sha="a" * 40)


@pytest.mark.asyncio
async def test_alias_manual_and_auto_share_one_session_effect(doctor):
    entered, finish = asyncio.Event(), asyncio.Event()
    calls = []

    async def effect(*args, ownership_token, **kwargs):
        calls.append(ownership_token)
        async with owned(ownership_token) as tx:
            sid = tx.reserve_session()
        entered.set()
        await finish.wait()
        async with owned(ownership_token) as tx:
            tx.bind_session(sid)
        return "spawned", sid, None

    doctor._pr_doctor_session_fix_owned = effect
    first = asyncio.create_task(doctor._pr_doctor_session_fix("o/r", "b", info(), "failing_ci", None))
    await entered.wait()
    assert (await doctor._pr_doctor_session_fix("renamed/r", "b", info(), "failing_ci", None,
                                               on_phase=lambda _: None))[0] == "transient"
    finish.set()
    assert (await first)[0] == "spawned"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_claimed_overlap_excludes_single_pr_and_partial_consolidation(doctor):
    from server.automations.ownership import OwnershipConflict
    await doctor._pr_doctor_claim("o/r", [1, 2], "consolidate:o/r#1,2")
    for refs, target in [([2], "o/r#2"), ([2, 3], "consolidate:o/r#2,3")]:
        with pytest.raises(OwnershipConflict):
            await doctor._pr_doctor_claim("o/r", refs, target)


@pytest.mark.asyncio
async def test_uncertain_mechanical_call_retains_owner_and_prevents_session(doctor, ownership_db, monkeypatch):
    from server.automations import mechanical_update as update
    from server.tests.test_mechanical_update_ownership import payload, proc
    async def request(repo, args):
        return proc({}, 1, 'lost response') if '-X' in args else proc(payload())
    monkeypatch.setattr(update, 'request', request)
    assert not await doctor._pr_doctor_mechanical("o/r", info(), "out_of_date")
    doctor._pr_doctor_session_fix_owned = AsyncMock()
    assert (await doctor._pr_doctor_session_fix("o/r", "b", info(), "failing_ci", None))[0] == "transient"
    doctor._pr_doctor_session_fix_owned.assert_not_awaited()
    async with ownership_db() as db:
        op = (await db.scalars(select(AutomationOperation))).one()
        assert op.phase == "uncertain"


@pytest.mark.asyncio
async def test_same_claimed_operation_cannot_be_dispatched_twice(doctor):
    from server.automations.ownership import OwnershipConflict
    await doctor._pr_doctor_claim("o/r", [1], "o/r#1")
    with pytest.raises(OwnershipConflict):
        await doctor._pr_doctor_claim("o/r", [1], "o/r#1")


@pytest.mark.asyncio
@pytest.mark.parametrize("coverage,linked,proof,expected", [
    (False, True, True, False), (True, False, True, False),
    (True, True, False, False), (True, True, True, True),
])
async def test_runner_handoff_requires_coverage_entire_batch_and_native_proof(
        doctor, ownership_db, monkeypatch, coverage, linked, proof, expected):
    from server.automations.ownership import acquire
    from server.db.models import AutomationTargetState
    async with ownership_db() as db:
        db.add(Automation(id="backlog-runner"))
        await db.commit()
    issue = CanonicalTarget("I_1", "o/r#1")
    pr = CanonicalTarget("PR_2", "o/r#2")
    async with acquire([issue], owner="backlog-runner", source="automatic", reason="implementation") as tx:
        token = tx.token
        sid = tx.reserve_session()
        tx.bind_session(sid)
        tx.operation.execution_generation = 1
        tx.operation.stopped_generation = 1 if proof else None
        state = {"ownership_token": token, "session_id": sid, "claimed": "yes", "attempts": 2}
        tx.db.add(AutomationTargetState(automation_id="backlog-runner", target="o/r#1", state=state))
    monkeypatch.setattr("server.automations.target_identity.resolve_targets",
                        AsyncMock(return_value=(pr, issue) if linked else (pr,)))
    doctor._backlog_ctx = AutomationContext("backlog-runner")
    doctor._backlog_ctx.emit = AsyncMock()
    doctor._backlog_pr_doctor_active = AsyncMock(return_value=coverage)
    doctor._pr_doctor_config = AsyncMock(return_value={"auto_fix_ci": True})
    # Use the canonical engine's actual permission key, rather than a fake planner.
    from server.automations.pr_doctor_engine import _AUTO_FLAG
    doctor._pr_doctor_config.return_value = {_AUTO_FLAG["failing_ci"]: True}
    assert await doctor._backlog_handoff("o/r#1", state, info(2)) is expected
    after = await doctor._backlog_ctx.get_target_state("o/r#1")
    if expected:
        assert after["ownership_token"] != token
        assert after["attempts"] == 2 and after["session_id"] is None
        assert after["implementation_session_id"] == sid
        assert (await doctor._pr_doctor_ctx.get_target_state("o/r#2"))["ownership_token"] == after["ownership_token"]
    else:
        assert after == state


@pytest.mark.asyncio
async def test_github_202_acceptance_retains_exclusion_against_consolidation(doctor, ownership_db, monkeypatch):
    from server.automations import mechanical_update as update
    from server.tests.test_mechanical_update_ownership import payload, proc, HEAD
    from server.automations.ownership import OwnershipConflict
    calls = []

    async def request(repo, argv):
        calls.append(argv)
        return proc({'message': 'Updating pull request branch'}) if '-X' in argv else proc(payload())

    monkeypatch.setattr(update, 'request', request)
    assert await doctor._pr_doctor_mechanical("o/r", info(), "out_of_date")
    assert f"expected_head_sha={HEAD}" in calls[1]
    with pytest.raises(OwnershipConflict):
        await doctor._pr_doctor_claim("o/r", [1, 2], "consolidate:o/r#1,2")
    async with ownership_db() as db:
        operation = (await db.scalars(select(AutomationOperation))).one()
        assert operation.phase == "uncertain"
        assert "pending" in operation.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["off", "propose", "disabled", "scope", "schedule", "engine", "ready"])
async def test_effective_coverage_uses_live_policy_and_schedule(doctor, ownership_db, case):
    from datetime import datetime, timezone
    async with ownership_db() as db:
        row = await db.get(Automation, "pr-doctor")
        row.enabled = case != "disabled"
        row.mode = case if case in ("off", "propose") else "auto"
        row.config = {"enabled_repos": ["different/repo"]} if case == "scope" else {}
        if case == "schedule":
            row.schedule = {"activeHours": {"start": "00:00", "end": "24:00",
                            "days": [(datetime.now(timezone.utc).weekday() + 1) % 7]}}
        await db.commit()
    doctor._pr_doctor_ctx.launch_plan_or_refuse = AsyncMock(return_value=None if case == "engine" else ("claude", {}))
    assert await doctor._backlog_pr_doctor_active("o/r") is (case == "ready")


async def _finished_runner_closeout(ownership_db, issues, *, owner="backlog-runner",
                                    finished=True, runner_target="o/r#1"):
    """A Backlog Runner ``terminal-closeout`` operation left in phase
    ``completed`` (the merged-PR close-out escalated to a human), retaining
    the batch's issue keys with no session attached — the live shape behind
    cairnea/KinDash#16440/#16465/#16489/#16490/#16492/#16498/#16512/#16538 on
    2026-09-22. ``finished=False`` leaves a session bound and the op ``active``."""
    from server.automations.ownership import acquire
    from server.db.models import AutomationTargetState
    async with ownership_db() as db:
        db.add(Automation(id="backlog-runner"))
        await db.commit()
    async with acquire(issues, owner=owner, source="terminal-closeout",
                       reason="merged PR close-out",
                       provenance={"origin_ref": runner_target,
                                   "implementation_session_id": "impl-sid"}) as tx:
        token = tx.token
        if finished:
            tx.start_action()
            tx.complete_action()
        else:
            sid = tx.reserve_session()
            tx.bind_session(sid)
        state = {"ownership_token": token, "session_id": "impl-sid", "claimed": False,
                 "gave_up": True, "held": True, "closeout_started": True, "closing_pr": 99,
                 "issues": [t.display_ref for t in issues], "attempts": 0}
        tx.db.add(AutomationTargetState(automation_id="backlog-runner", target=runner_target,
                                        state=state))
    return token, state


@pytest.mark.asyncio
async def test_claim_adopts_finished_runner_closeout_holding_the_linked_issue(
        doctor, ownership_db, monkeypatch):
    """A finished runner close-out has no live actor; it must not orphan the PR
    that closes its issue. PR Doctor claims the PR by taking the close-out over
    as a ``runner-handoff`` successor covering PR + issue."""
    issue, pr = CanonicalTarget("I_1", "o/r#1"), CanonicalTarget("PR_2", "o/r#2")
    token, runner_state = await _finished_runner_closeout(ownership_db, [issue])
    monkeypatch.setattr("server.automations.target_identity.resolve_targets",
                        AsyncMock(return_value=(pr, issue)))
    state = await doctor._pr_doctor_claim("o/r", [2], "o/r#2")
    successor = state["ownership_token"]
    assert successor and successor != token
    async with ownership_db() as db:
        op = await db.get(AutomationOperation, successor)
        assert (op.owner, op.source, op.phase) == ("pr-doctor", "runner-handoff", "claimed")
        assert set(op.targets) == {issue.key, pr.key}
        assert op.provenance["runner_target"] == "o/r#1"
        assert op.provenance["dispatch_claimed"] is True
        assert op.predecessor_token == token
        assert (await db.get(AutomationOperation, token)).phase == "transferred"
    runner_after = await AutomationContext("backlog-runner").get_target_state("o/r#1")
    assert runner_after["ownership_token"] == successor
    assert runner_after["handed_off"] is True and runner_after["session_id"] is None
    assert runner_after["closeout_started"] is True  # the runner stays parked, never re-attributes
    assert (await doctor._pr_doctor_ctx.get_target_state("o/r#2"))["ownership_token"] == successor


async def _finished_self_handoff(ownership_db, targets, *, origin_ref, runner_target="o/r#1"):
    """PR Doctor's OWN finished ``runner-handoff`` operation for *origin_ref* —
    the shape every merged handed-off PR leaves behind until the runner's
    close-out takes it back: ``completed``, no session, still holding the
    batch's issue keys."""
    from server.automations.ownership import acquire
    async with acquire(targets, owner="pr-doctor", source="runner-handoff",
                       reason="stopped implementation transferred to authorized PR recovery",
                       provenance={"origin_ref": origin_ref, "runner_target": runner_target,
                                   "dispatch_claimed": True}) as tx:
        tx.start_action()
        tx.complete_action()
        return tx.token


@pytest.mark.asyncio
async def test_claim_names_a_stale_self_owned_handoff_holding_the_linked_issue(
        doctor, ownership_db, monkeypatch):
    """cairnea/KinDash#17006 (2026-09-27): its closing issue was still held by PR
    Doctor's own finished ``runner-handoff`` op for #16889, merged three days
    earlier. Owner ``pr-doctor`` is not "own lifecycle" when the op belongs to
    ANOTHER PR — the refusal must name that holder, and the read-only
    actionable-items check must agree so the button is not offered live."""
    from server.automations.ownership import OwnershipConflict
    issue, other_pr, pr = (CanonicalTarget("I_1", "o/r#1"), CanonicalTarget("PR_9", "o/r#9"),
                           CanonicalTarget("PR_2", "o/r#2"))
    token = await _finished_self_handoff(ownership_db, [issue, other_pr], origin_ref="o/r#9")
    monkeypatch.setattr("server.automations.target_identity.resolve_targets",
                        AsyncMock(return_value=(pr, issue)))
    with pytest.raises(OwnershipConflict) as excinfo:
        await doctor._pr_doctor_claim("o/r", [2], "o/r#2")
    assert str(excinfo.value) == "Held by PR Doctor runner-handoff of o/r#9 (needs human)"
    blocker = await doctor._pr_doctor_foreign_operation((pr, issue), target="o/r#2")
    assert blocker is not None and blocker.token == token
    async with ownership_db() as db:
        assert (await db.get(AutomationOperation, token)).phase == "completed"


@pytest.mark.asyncio
async def test_own_finished_handoff_is_not_reported_as_a_foreign_holder(
        doctor, ownership_db):
    """The PR's OWN predecessor (same ``origin_ref``) is the transfer path
    ``_pr_doctor_claim`` already handles through the state token — reporting it
    as a foreign holder would hide the button for a claim that would succeed."""
    issue, pr = CanonicalTarget("I_1", "o/r#1"), CanonicalTarget("PR_2", "o/r#2")
    await _finished_self_handoff(ownership_db, [issue, pr], origin_ref="o/r#2")
    assert await doctor._pr_doctor_foreign_operation((pr, issue), target="o/r#2") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("case,expected", [
    # #2203: not adoptable → name the actual holder instead of the generic
    # "target already has an operation", so the manual button and the feed's
    # ownership_wait event both say WHO and WHY.
    ("live_runner_session", "Held by Backlog Runner close-out of o/r#1 — in progress, try again shortly"),
    ("partial_batch", "Held by Backlog Runner close-out of o/r#1 (needs human)"),
    ("foreign_owner", "Held by Error Triage close-out of o/r#1 (needs human)"),
])
async def test_claim_refuses_runner_work_that_is_live_partial_or_foreign(
        doctor, ownership_db, monkeypatch, case, expected):
    from server.automations.ownership import OwnershipConflict
    issue, other, pr = (CanonicalTarget("I_1", "o/r#1"), CanonicalTarget("I_3", "o/r#3"),
                        CanonicalTarget("PR_2", "o/r#2"))
    if case == "live_runner_session":
        token, _ = await _finished_runner_closeout(ownership_db, [issue], finished=False)
    elif case == "partial_batch":
        token, _ = await _finished_runner_closeout(ownership_db, [issue, other])
    else:
        token, _ = await _finished_runner_closeout(ownership_db, [issue], owner="error-triage")
    monkeypatch.setattr("server.automations.target_identity.resolve_targets",
                        AsyncMock(return_value=(pr, issue)))
    with pytest.raises(OwnershipConflict) as excinfo:
        await doctor._pr_doctor_claim("o/r", [2], "o/r#2")
    assert str(excinfo.value) == expected
    async with ownership_db() as db:
        assert (await db.get(AutomationOperation, token)).phase in ("completed", "active")
    assert (await AutomationContext("backlog-runner").get_target_state("o/r#1"))["ownership_token"] == token
