"""Runner overlap and stale-decision regressions using actual SQL transactions."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from server.automations.backlog_runner import BacklogRunnerLoop
from server.automations.backlog_selection import Batch
from server.automations.context import AutomationContext
from server.automations.ownership import CanonicalTarget, OwnershipConflict, ReleaseProof, owned
from server.db.models import Automation, AutomationOperation, AutomationOwnedTarget
from server.tests.test_automation_ownership import ownership_db as ownership_db


@pytest_asyncio.fixture
async def runner(ownership_db, monkeypatch):
    async with ownership_db() as db:
        db.add(Automation(id="backlog-runner"))
        await db.commit()

    async def resolve(refs, *, kind):
        assert kind == "issue"
        return tuple(CanonicalTarget("I_" + ref.split("#")[1], ref) for ref in refs)

    monkeypatch.setattr("server.automations.target_identity.resolve_targets", resolve)
    # This fixture has explicitly seeded no legacy population. Production always
    # invokes the real cross-origin guard; its own tests cover legacy evidence.
    monkeypatch.setattr("server.automations.ownership_reconciliation.reconcile_legacy_targets",
                        AsyncMock())
    ctx = AutomationContext("backlog-runner")
    ctx.emit = AsyncMock()

    async def spawn(batch, account, *, default_model, ownership_token):
        async with owned(ownership_token) as tx:
            sid = tx.reserve_session()
        async with owned(ownership_token) as tx:
            tx.bind_session(sid)
        return SimpleNamespace(id=sid, branch="work", name="issue", model=default_model)

    loop = BacklogRunnerLoop(
        ctx, select_batch=AsyncMock(return_value=None), spawn=spawn,
        count_in_flight=AsyncMock(return_value=0), healthy_account=AsyncMock(return_value="a"),
        pr_status_for=AsyncMock(return_value=None), pr_doctor_active=AsyncMock(return_value=False),
        read_config=AsyncMock(return_value=("on", {})), clock=lambda: 1,
        session_work_stopped=AsyncMock(return_value=False),
        admission=AsyncMock(return_value=(
            SimpleNamespace(permitted=True, product_slug=None, configured_dispatch="auto"),
            {"default_model": "auto"})))
    ctx.record_outcome = AsyncMock()
    return loop, ctx


def batch(*refs):
    return Batch(primary=refs[0], issues=list(refs), repo=refs[0].split("#")[0], product=None)


async def dispatch(loop, *refs, **kw):
    return await loop.dispatch_batch(batch(*refs), source=kw.pop("source", "manual"),
                                     account=kw.pop("account", "a"), default_model="sonnet", **kw)


@pytest.mark.asyncio
async def test_overlapping_batches_have_one_owner_and_no_partial_claim(runner, ownership_db):
    loop, ctx = runner
    first, second = await asyncio.gather(
        dispatch(loop, "o/r#1", "o/r#2"), dispatch(loop, "o/r#3", "o/r#2"))
    assert sorted([first["status"], second["status"]]) == ["busy", "spawned"]
    states = await ctx.list_target_states()
    assert len(states) == 1
    async with ownership_db() as db:
        targets = list((await db.scalars(select(AutomationOwnedTarget))).all())
        assert len(targets) == 2
        assert len({row.operation_token for row in targets}) == 1


@pytest.mark.asyncio
async def test_alias_and_automatic_dispatch_wait_for_manual(runner):
    loop, _ = runner
    assert (await dispatch(loop, "old/repo#1"))["status"] == "spawned"
    assert (await dispatch(loop, "renamed/Repo#1", source="automatic"))["status"] == "busy"


@pytest.mark.asyncio
async def test_proposal_tap_starts_entire_existing_operation(runner):
    loop, ctx = runner
    assert (await dispatch(loop, "o/r#1", "o/r#2", propose=True))["status"] == "proposed"
    before = await ctx.get_target_state("o/r#1")
    assert (await dispatch(loop, "o/r#1", "o/r#2", source="proposal"))["status"] == "spawned"
    after = await ctx.get_target_state("o/r#1")
    assert after["ownership_token"] == before["ownership_token"]
    assert after["issues"] == ["o/r#1", "o/r#2"]
    assert "proposed" not in after


@pytest.mark.asyncio
async def test_no_account_keeps_claim_and_retry_uses_same_token(runner):
    loop, ctx = runner
    assert (await dispatch(loop, "o/r#1", account=None))["status"] == "no_account"
    before = await ctx.get_target_state("o/r#1")
    assert (await dispatch(loop, "o/r#1"))["status"] == "spawned"
    assert (await ctx.get_target_state("o/r#1"))["ownership_token"] == before["ownership_token"]


@pytest.mark.asyncio
async def test_unknown_launch_and_restart_never_release_reserved_session(runner, ownership_db):
    loop, ctx = runner

    async def unknown(*a, ownership_token, **kw):
        async with owned(ownership_token) as tx:
            tx.reserve_session()
        raise TimeoutError("RPC result lost")

    loop._spawn = unknown
    assert (await dispatch(loop, "o/r#1"))["status"] == "uncertain"
    before = await ctx.get_target_state("o/r#1")
    await loop._reconcile_dangling()
    assert await ctx.get_target_state("o/r#1") == before
    assert (await dispatch(loop, "o/r#1"))["status"] == "busy"
    async with ownership_db() as db:
        operations = list((await db.scalars(select(AutomationOperation))).all())
        assert len(operations) == 1
        assert operations[0].reserved_session_id


@pytest.mark.asyncio
async def test_policy_refusal_releases_only_unstarted_claim(runner):
    loop, ctx = runner
    loop._spawn = AsyncMock(return_value=None)
    assert (await dispatch(loop, "o/r#1"))["status"] == "refused"
    assert await ctx.get_target_state("o/r#1") == {}


@pytest.mark.asyncio
async def test_sweep_read_barrier_cannot_overwrite_operator_change(runner):
    loop, ctx = runner
    await dispatch(loop, "o/r#1")
    before = await ctx.get_target_state("o/r#1")
    read = asyncio.Event()
    resume = asyncio.Event()

    async def status(_target):
        read.set()
        await resume.wait()
        return None

    loop._pr_status_for = status
    loop._session_work_stopped = AsyncMock(return_value=True)
    loop._session_ended = AsyncMock(return_value=True)
    sweep = asyncio.create_task(loop._sweep_one("o/r#1", before, set()))
    await read.wait()
    changed = {**before, "gave_up": True, "reason": "human correction"}
    assert await ctx.compare_target_state("o/r#1", before, changed)
    resume.set()
    await sweep
    assert await ctx.get_target_state("o/r#1") == changed


@pytest.mark.asyncio
async def test_stale_token_cannot_delete_successor_state(runner):
    loop, ctx = runner
    await dispatch(loop, "o/r#1", propose=True)
    before = await ctx.get_target_state("o/r#1")
    async with owned(before["ownership_token"]) as tx:
        successor = await tx.transfer(owner="pr-doctor", source="handoff",
                                      proof=ReleaseProof.NOT_STARTED, reason="test handoff")
    assert not await ctx.compare_target_state("o/r#1", before, None)
    async with owned(successor):
        pass


@pytest.mark.asyncio
async def test_database_error_fails_closed_without_spawn(runner, monkeypatch):
    loop, ctx = runner
    monkeypatch.setattr(ctx, "claim_target_state", AsyncMock(side_effect=OSError("DB unavailable")))
    loop._spawn = AsyncMock()
    with pytest.raises(OSError, match="DB unavailable"):
        await dispatch(loop, "o/r#1")
    loop._spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_proposal_subset_cannot_drop_secondary_ownership(runner):
    loop, ctx = runner
    await dispatch(loop, "o/r#1", "o/r#2", propose=True)
    before = await ctx.get_target_state("o/r#1")
    with pytest.raises(OwnershipConflict):
        await ctx.claim_target_state("o/r#1", {"issues": ["o/r#1"]},
                                     expected=before, source="proposal")
    assert await ctx.get_target_state("o/r#1") == before


@pytest.mark.asyncio
async def test_stopped_retry_rotates_token_and_preserves_attempts(runner):
    from server.automations.ownership import begin_session_execution, attest_session_stopped
    loop, ctx = runner
    first = await dispatch(loop, "o/r#1")
    await begin_session_execution(first["session_id"])
    before = await ctx.get_target_state("o/r#1")
    assert await attest_session_stopped(before["ownership_token"], 1)
    ended = {**before, "_ended": True, "attempts": 2}
    assert await ctx.compare_target_state("o/r#1", before, ended)
    loop._session_work_stopped = AsyncMock(return_value=True)
    assert (await dispatch(loop, "o/r#1"))["status"] == "spawned"
    after = await ctx.get_target_state("o/r#1")
    assert after["ownership_token"] != before["ownership_token"]
    assert after["attempts"] == 2
    assert not await ctx.compare_target_state("o/r#1", ended, None)


@pytest.mark.asyncio
async def test_restart_after_stop_observation_prevents_terminal_release(runner):
    from server.automations.ownership import begin_session_execution, attest_session_stopped
    loop, ctx = runner
    first = await dispatch(loop, "o/r#1")
    await begin_session_execution(first["session_id"])
    before = await ctx.get_target_state("o/r#1")

    async def stale_proof(_sid):
        assert await attest_session_stopped(before["ownership_token"], 1)
        await begin_session_execution(first["session_id"])
        return True

    loop._session_work_stopped = stale_proof
    with pytest.raises(ValueError):
        await loop._write_state("o/r#1", before, None)
    assert await ctx.get_target_state("o/r#1") == before


@pytest.mark.asyncio
async def test_expired_human_wait_stale_snapshot_cannot_delete_manual_retry(runner):
    from server.automations.ownership import begin_session_execution, attest_session_stopped
    loop, ctx = runner
    first = await dispatch(loop, "o/r#1")
    await begin_session_execution(first["session_id"])
    before = await ctx.get_target_state("o/r#1")
    assert await attest_session_stopped(before["ownership_token"], 1)
    parked = {**before, "gave_up": True, "claimed": False, "reason": "test"}
    assert await ctx.compare_target_state("o/r#1", before, parked)
    loop._session_work_stopped = AsyncMock(return_value=True)
    assert (await dispatch(loop, "o/r#1"))["status"] == "spawned"
    after = await ctx.get_target_state("o/r#1")
    assert not await loop._write_state("o/r#1", parked, None)
    assert await ctx.get_target_state("o/r#1") == after


@pytest.mark.asyncio
async def test_closeout_transfer_fences_pr_actor_before_external_call(runner):
    from server.automations.ownership import (
        begin_session_execution, attest_session_stopped, StaleOwnership,
    )
    from server.github.pr_status import PullRequestInfo
    loop, ctx = runner
    first = await dispatch(loop, "o/r#1")
    await begin_session_execution(first["session_id"])
    before = await ctx.get_target_state("o/r#1")
    assert await attest_session_stopped(before["ownership_token"], 1)
    loop._session_work_stopped = AsyncMock(return_value=True)
    loop._pr_status_for = AsyncMock(return_value=PullRequestInfo(
        number=9, url="", state="merged", checks="none", title="", merged=True))
    ctx.finalize_outcome = AsyncMock()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def close_out(target, info):
        entered.set()
        await finish.wait()
        return "closed"

    loop._close_out = close_out
    sweep = asyncio.create_task(loop._sweep_one("o/r#1", before, set()))
    await asyncio.wait_for(entered.wait(), 5)
    with pytest.raises(StaleOwnership):
        async with owned(before["ownership_token"]):
            pass
    assert (await dispatch(loop, "o/r#1"))["status"] == "busy"
    finish.set()
    await sweep
    assert await ctx.get_target_state("o/r#1") == {}
    ctx.finalize_outcome.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_closeout_retains_actor_without_replaying(runner):
    from server.github.pr_status import PullRequestInfo
    loop, ctx = runner
    # A proposal has not started native work, providing NOT_STARTED proof.
    await dispatch(loop, "o/r#1", propose=True)
    before = await ctx.get_target_state("o/r#1")
    state = {k: v for k, v in before.items() if k != "proposed"}
    assert await ctx.compare_target_state("o/r#1", before, state)
    loop._work_stopped = AsyncMock(return_value=True)
    loop._pr_status_for = AsyncMock(return_value=PullRequestInfo(
        number=9, url="", state="merged", checks="none", title="", merged=True))
    loop._close_out = AsyncMock(return_value="retry")
    await loop._sweep_one("o/r#1", state, set())
    after = await ctx.get_target_state("o/r#1")
    assert after["closeout_started"] and after["ownership_wait_reason"]
    await loop._sweep_one("o/r#1", after, set())
    loop._close_out.assert_awaited_once()
    assert (await dispatch(loop, "o/r#1"))["status"] == "busy"


@pytest.mark.asyncio
async def test_monitor_ignores_predecessor_stop_proof_for_active_successor(runner):
    loop, ctx = runner
    await dispatch(loop, "o/r#1", propose=True)
    before = await ctx.get_target_state("o/r#1")
    async with owned(before["ownership_token"]) as tx:
        successor = await tx.transfer(owner="pr-doctor", source="runner-handoff",
                                      proof=ReleaseProof.NOT_STARTED, reason="handoff")
    state = {**before, "ownership_token": successor, "handed_off": True,
             "session_id": None, "implementation_session_id": "old-implementation"}
    async with owned(successor) as tx:
        tx.start_action()
    loop._session_work_stopped = AsyncMock(return_value=True)
    assert not await loop._work_stopped(state)
    loop._session_work_stopped.assert_not_awaited()
    async with owned(successor) as tx:
        tx.complete_action()
    assert await loop._work_stopped(state)


@pytest.mark.asyncio
async def test_work_stopped_trusts_an_archived_session_over_a_stuck_generation_proof(runner):
    """#2202: a claim whose owning session the daemon itself archived is proof
    enough on its own — the managed-tree generation proof (``_session_work_stopped``)
    can get stuck indefinitely (steiner385/viberpit#2202's own claim outlived its
    archived session 24564d1d this way) and must not be the only path that can
    release the slot."""
    loop, ctx = runner
    await dispatch(loop, "o/r#1")
    state = await ctx.get_target_state("o/r#1")
    loop._session_work_stopped = AsyncMock(return_value=False)
    loop._session_gone = AsyncMock(return_value=True)
    assert await loop._work_stopped(state)
    loop._session_gone.assert_awaited_once_with(state["session_id"])
    loop._session_work_stopped.assert_not_awaited()


@pytest.mark.asyncio
async def test_work_stopped_falls_back_to_native_proof_when_session_not_gone(runner):
    """A session that is still live (or unknown) gets no shortcut — the sweep
    keeps relying on the real managed-tree proof, same as before this claim's
    session ever becomes terminal."""
    loop, ctx = runner
    await dispatch(loop, "o/r#1")
    state = await ctx.get_target_state("o/r#1")
    loop._session_work_stopped = AsyncMock(return_value=True)
    loop._session_gone = AsyncMock(return_value=False)
    assert await loop._work_stopped(state)
    loop._session_gone.assert_awaited_once_with(state["session_id"])
    loop._session_work_stopped.assert_awaited_once_with(state["session_id"])


@pytest.mark.asyncio
async def test_work_stopped_repairs_the_generation_proof_when_session_gone(runner):
    """#2202's own claim outliving its archived session was only half the story
    (steiner385/viberpit#2202, re-opened as #2232/#2233): trusting
    ``_session_gone`` makes ``_work_stopped`` answer True, but the sweep's
    actual release (``reserve_target_action`` -> ``tx.transfer(WORK_STOPPED)``)
    independently re-derives "stopped" from ``stopped_generation ==
    execution_generation`` in ``ownership.py:_validate_release`` — unaware of
    the DB-status shortcut. A stuck native proof never converges, so the
    release itself kept raising "native session execution has no current
    stopped-work attestation" every sweep tick forever. ``_session_terminal_attest``
    must be given the chance to repair that proof before ``_work_stopped``
    returns."""
    loop, ctx = runner
    await dispatch(loop, "o/r#1")
    state = await ctx.get_target_state("o/r#1")
    loop._session_gone = AsyncMock(return_value=True)
    loop._session_terminal_attest = AsyncMock(return_value=True)
    assert await loop._work_stopped(state)
    loop._session_terminal_attest.assert_awaited_once_with(state["session_id"])


@pytest.mark.asyncio
async def test_terminal_attest_repairs_a_stuck_proof_so_release_actually_succeeds(runner):
    """End-to-end reproduction of production's exact failure and its fix: a
    session whose one turn started (generation bumped to 1) but whose close was
    never individually attested — the shape a WORKER-hosted session's archived
    row leaves behind, since the primary sweep cannot vouch for a driver it never
    ran (``_automation_session_work_stopped``'s host-stamp gate correctly
    refuses). Before the repair, the real fenced release raises the production
    ValueError verbatim. ``attest_terminal_session_stopped`` is pure DB CAS with
    no host gate, so wiring it as ``_session_terminal_attest`` lets the same
    release succeed afterwards."""
    from server.automations.ownership import attest_terminal_session_stopped
    from server.db.models import Session as DbSession

    loop, ctx = runner
    await dispatch(loop, "o/r#1")
    state = await ctx.get_target_state("o/r#1")
    sid = state["session_id"]
    async with owned(state["ownership_token"]) as tx:
        tx.operation.execution_generation = 1
        tx.operation.execution_sessions = {
            sid: {"current": "exec-1", "executions": {
                "exec-1": {"generation": 1, "stopped": None}}}}
        tx.db.add(DbSession(id=sid, name="archived", cwd="/tmp", account_name="a",
                            origin="backlog-runner", origin_ref="o/r#1", status="archived"))

    with pytest.raises(ValueError, match="stopped-work attestation"):
        async with owned(state["ownership_token"]) as tx:
            await tx.transfer(owner="backlog-runner", source="terminal-closeout",
                              proof=ReleaseProof.WORK_STOPPED, reason="merged PR close-out")

    loop._session_gone = AsyncMock(return_value=True)
    loop._session_terminal_attest = attest_terminal_session_stopped
    assert await loop._work_stopped(state)

    async with owned(state["ownership_token"]) as tx:
        await tx.transfer(owner="backlog-runner", source="terminal-closeout",
                          proof=ReleaseProof.WORK_STOPPED, reason="merged PR close-out")


@pytest.mark.asyncio
async def test_lost_closeout_response_recovers_only_all_verified_closed_targets(runner, monkeypatch):
    from server.sessions.orchestrator import SessionOrchestrator
    loop, ctx = runner
    await dispatch(loop, "o/r#1", "o/r#2", propose=True)
    before = await ctx.get_target_state("o/r#1")
    state = {k: v for k, v in before.items() if k != "proposed"}
    closing = await ctx.reserve_target_action(
        "o/r#1", before, source="terminal-closeout", reason="test lost result",
        state={**state, "closing_pr": 9, "closeout_started": True})
    assert closing is not None
    observed = {"I_1": "CLOSED", "I_2": "OPEN"}

    async def fetch(owner, query):
        node = next(node for node in observed if node in query)
        return {"data": {"node": {"id": node, "state": observed[node]}}}

    monkeypatch.setattr("server.automations.target_identity._fetch", fetch)
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    loop._closeout_completed = orch._backlog_closeout_completed
    loop._close_out = AsyncMock()
    ctx.finalize_outcome = AsyncMock()
    await loop._sweep_one("o/r#1", closing, set())
    assert await ctx.get_target_state("o/r#1") == closing
    observed["I_2"] = "CLOSED"
    await loop._sweep_one("o/r#1", closing, set())
    assert await ctx.get_target_state("o/r#1") == {}
    loop._close_out.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("successful_closes", [set(), {"1"}])
async def test_real_closeout_helper_failure_retains_sweep_ownership(
        runner, monkeypatch, successful_closes):
    import subprocess
    from server.automations import backlog_github
    from server.github.pr_status import PullRequestInfo
    loop, ctx = runner
    await dispatch(loop, "o/r#1", "o/r#2", propose=True)
    before = await ctx.get_target_state("o/r#1")
    state = {key: value for key, value in before.items() if key != "proposed"}
    assert await ctx.compare_target_state("o/r#1", before, state)
    loop._work_stopped = AsyncMock(return_value=True)
    info = PullRequestInfo(number=9, url="", state="merged", checks="none", title="", merged=True)
    loop._pr_status_for = AsyncMock(return_value=info)
    monkeypatch.setattr(backlog_github, "_pr_linkage", lambda *a, **kw: {})
    monkeypatch.setattr(backlog_github, "_pr_issue_linked", lambda *a, **kw: True)
    monkeypatch.setattr(backlog_github, "_pr_changed_files", lambda *a, **kw: ["app.py"])
    monkeypatch.setattr(backlog_github, "_issue_signals", lambda *a, **kw: {})
    monkeypatch.setattr(backlog_github.gitrepos, "app_token_for_repo", lambda *a: None)
    calls = []

    def gh_run(args, **kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0 if args[2] in successful_closes else 1, "", "write failed")

    async def close_out(target, pr):
        return backlog_github.close_batch_issues(batch("o/r#1", "o/r#2"), pr, gh_run=gh_run)

    loop._close_out = close_out
    ctx.finalize_outcome = AsyncMock()
    await loop._sweep_one("o/r#1", state, set())
    after = await ctx.get_target_state("o/r#1")
    assert len(calls) == 2
    assert after["closeout_started"] and after["ownership_wait_reason"]
    async with owned(after["ownership_token"]) as tx:
        assert tx.operation.phase in {"active", "uncertain"}
    ctx.finalize_outcome.assert_not_awaited()
    assert (await dispatch(loop, "o/r#1", "o/r#2"))["status"] == "busy"


@pytest.mark.asyncio
@pytest.mark.parametrize("origin,origin_ref", [
    ("backlog-runner", "o/r#1"), ("other-actor", "o/r#1"), ("backlog-runner", "o/r#2"),
])
async def test_reconcile_persisted_prebind_launch_validates_provenance(
        runner, origin, origin_ref):
    from server.automations.ownership import begin_session_execution
    from server.db.models import Session as DbSession
    loop, ctx = runner
    started = []

    async def crashed(*a, ownership_token, **kw):
        async with owned(ownership_token) as tx:
            sid = tx.reserve_session()
        async with owned(ownership_token) as tx:
            tx.db.add(DbSession(id=sid, name="reserved", cwd="/tmp", account_name="a",
                                origin=origin, origin_ref=origin_ref, repo="o/r", status="working"))
        await begin_session_execution(sid)
        started.append(sid)
        raise TimeoutError("native launch persisted; binding response lost")

    loop._spawn = crashed
    assert (await dispatch(loop, "o/r#1"))["status"] == "uncertain"
    before = await ctx.get_target_state("o/r#1")
    assert not before.get("session_id")
    await loop._reconcile_dangling()
    state = await ctx.get_target_state("o/r#1")
    valid = origin == "backlog-runner" and origin_ref == "o/r#1"
    if valid:
        assert state["session_id"] == started[0]
        assert len(loop._spawns_today) == 1
        ctx.record_outcome.assert_awaited_once()
        async with owned(state["ownership_token"]) as tx:
            assert tx.operation.session_id == started[0] and tx.operation.phase == "active"
        await loop._sweep_one("o/r#1", state, set())
        loop._session_work_stopped.assert_awaited_once_with(started[0])
        assert (await dispatch(loop, "o/r#1"))["status"] == "busy"
        assert len(started) == 1
    else:
        assert state == before
        async with owned(state["ownership_token"]) as tx:
            assert tx.operation.session_id is None


@pytest.mark.asyncio
async def test_prebind_reconcile_cannot_overwrite_changed_state_snapshot(runner):
    from server.db.models import Session as DbSession
    loop, ctx = runner
    await dispatch(loop, "o/r#1", propose=True)
    proposal = await ctx.get_target_state("o/r#1")
    state = {k: v for k, v in proposal.items() if k != "proposed"}
    assert await ctx.compare_target_state("o/r#1", proposal, state)
    async with owned(state["ownership_token"]) as tx:
        sid = tx.reserve_session()
        tx.db.add(DbSession(id=sid, name="reserved", cwd="/tmp", account_name="a",
                            origin="backlog-runner", origin_ref="o/r#1"))
    snapshot = await ctx.list_target_states()
    changed = {**state, "reason": "human changed this work"}
    assert await ctx.compare_target_state("o/r#1", state, changed)
    ctx.list_target_states = AsyncMock(return_value=snapshot)
    await loop._reconcile_dangling()
    assert await ctx.get_target_state("o/r#1") == changed
    async with owned(state["ownership_token"]) as tx:
        assert tx.operation.session_id is None


@pytest.mark.asyncio
async def test_live_prebind_observer_and_callback_account_once(runner):
    from server.automations.ownership import begin_session_execution
    from server.db.models import Session as DbSession
    loop, ctx = runner
    persisted, resume = asyncio.Event(), asyncio.Event()

    async def healthy(*a, ownership_token, **kw):
        async with owned(ownership_token) as tx:
            sid = tx.reserve_session()
            tx.db.add(DbSession(id=sid, name="live", cwd="/tmp", account_name="a",
                                origin="backlog-runner", origin_ref="o/r#1", branch="work"))
        await begin_session_execution(sid)
        persisted.set()
        await resume.wait()
        async with owned(ownership_token) as tx:
            tx.bind_session(sid)
            with pytest.raises(ValueError):
                tx.bind_session("unrelated")
        return SimpleNamespace(id=sid, name="live", branch="work")

    loop._spawn = healthy
    launching = asyncio.create_task(dispatch(loop, "o/r#1"))
    await persisted.wait()
    try:
        await loop._reconcile_dangling()
        state = await ctx.get_target_state("o/r#1")
        assert state["session_id"]
        assert len(loop._spawns_today) == 1
        ctx.record_outcome.assert_awaited_once()
    finally:
        resume.set()
    result = await launching
    assert result["status"] == "spawned"
    assert result["session_id"] == state["session_id"]
    await loop._reconcile_dangling()
    assert len(loop._spawns_today) == 1
    ctx.record_outcome.assert_awaited_once()
    # A fresh process rebuilds its budget from the same retained operation.
    loop._spawns_today.clear()
    loop._accounted_dispatches.clear()
    await loop._refresh_dispatch_budget()
    await loop._refresh_dispatch_budget()
    assert len(loop._spawns_today) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("observer", [False, True])
@pytest.mark.parametrize("applied_model", ["sonnet", "opus", None])
async def test_dispatch_history_uses_applied_session_model(runner, observer, applied_model):
    from server.db.models import Session as DbSession
    loop, ctx = runner

    async def spawn(*a, ownership_token, **kw):
        async with owned(ownership_token) as tx:
            sid = tx.reserve_session()
            tx.db.add(DbSession(id=sid, name="selected model", cwd="/tmp", account_name="a",
                                origin="backlog-runner", origin_ref="o/r#1", model=applied_model))
        if observer:
            raise TimeoutError("callback lost")
        async with owned(ownership_token) as tx:
            tx.bind_session(sid)
        return SimpleNamespace(id=sid, branch="work", model="auto" if applied_model else None)

    loop._spawn = spawn
    result = await loop.dispatch_batch(batch("o/r#1"), source="automatic", account="a",
                                       default_model="auto")
    assert result["status"] == ("uncertain" if observer else "spawned")
    if observer:
        await loop._reconcile_dangling()
    ctx.record_outcome.assert_awaited_once()
    recorded = ctx.record_outcome.await_args
    assert recorded is not None
    assert recorded.kwargs["model"] == (applied_model or "auto")
    state = await ctx.get_target_state("o/r#1")
    async with owned(state["ownership_token"]) as tx:
        assert tx.operation.provenance["runner_dispatch"]["model"] == (applied_model or "auto")


@pytest.mark.asyncio
async def test_dispatch_history_preserves_callback_model_fallback(runner):
    loop, ctx = runner
    original = loop._spawn

    async def spawn(*args, **kwargs):
        rec = await original(*args, **kwargs)
        rec.model = "opus"
        return rec

    loop._spawn = spawn
    assert (await loop.dispatch_batch(batch("o/r#1"), source="automatic", account="a",
                                      default_model="auto"))["status"] == "spawned"
    recorded = ctx.record_outcome.await_args
    assert recorded is not None
    assert recorded.kwargs["model"] == "opus"
