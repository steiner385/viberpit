"""Native observation and durable ownership must agree before release."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.automations.ownership import (
    ReleaseProof, StaleOwnership, attest_session_stopped, begin_session_execution,
    owned, session_execution,
)
from server.sessions.automation_execution import AutomationExecutionFence
from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes import fake_sdk as f
from server.tests.test_automation_ownership import ownership_db as ownership_db, claim, target


def driver(client=None, **kwargs):
    client = client or f.FakeSDKClient([f.result()])
    return ClaudeAgentDriver("s", "account", "/tmp", "/tmp", mode="interactive",
                             sdk_factory=lambda *a, **k: client,
                             registry=PermissionRegistry(), identity="test", **kwargs)


@pytest.mark.asyncio
async def test_completed_native_turn_and_close_freeze_future_send():
    d = driver()
    await d.start()
    await d.send_prompt("hello")
    await d._pump
    assert await d.automation_work_stopped()
    with pytest.raises(RuntimeError, match="closed for ownership"):
        await d.send_prompt("resume after handoff")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("close failed"), asyncio.CancelledError()])
async def test_close_failure_or_child_cancellation_never_attests(failure):
    client = f.FakeSDKClient([f.result()])
    client.__aexit__ = AsyncMock(side_effect=failure)
    d = driver(client)
    await d.start()
    await d.send_prompt("hello")
    await d._pump
    assert not await d.automation_work_stopped()


@pytest.mark.asyncio
async def test_complete_before_drain_is_not_quiescent():
    d = driver()
    d._automation_terminal_observed = True
    d._turn_active = True
    assert not await d.automation_work_stopped()
    assert not d._automation_fence.frozen


@pytest.mark.asyncio
async def test_raw_background_task_lifecycle_must_resolve():
    d = driver()
    await d.start()
    await d.send_prompt("hello")
    await d._pump
    await d._handle_msg(f.task_started("bg", None, task_type="local_bash"))
    assert not await d.automation_work_stopped()
    await d._handle_msg(f.task_notification("bg", None, status="completed"))
    assert await d.automation_work_stopped()


@pytest.mark.asyncio
async def test_incomplete_resumed_task_history_is_unknown():
    d = driver(resume="prior-native-session")
    d._automation_terminal_observed = True
    assert not await d.automation_work_stopped()


@pytest.mark.asyncio
async def test_generation_changes_invalidate_proof_and_released_session_cannot_resume(ownership_db):
    token = await claim([target()])
    async with owned(token) as tx:
        sid = tx.reserve_session()
    await begin_session_execution(sid)
    await begin_session_execution(sid)
    assert not await attest_session_stopped(token, 1)
    assert await attest_session_stopped(token, 2)
    await begin_session_execution(sid)
    async with owned(token) as tx:
        with pytest.raises(ValueError, match="attestation"):
            tx.release(ReleaseProof.WORK_STOPPED, "old proof")
    assert await attest_session_stopped(token, 3)
    async with owned(token) as tx:
        await tx.transfer(owner="repair", source="pr", proof=ReleaseProof.WORK_STOPPED,
                          reason="native work stopped")
    with pytest.raises(StaleOwnership):
        await begin_session_execution(sid)


@pytest.mark.asyncio
async def test_missing_or_remote_driver_cannot_attest(ownership_db):
    token = await claim([target()])
    async with owned(token) as tx:
        sid = tx.reserve_session()
    await begin_session_execution(sid)
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {sid: SimpleNamespace(status="complete")}
    orch._drivers = {}
    assert not await orch._automation_session_work_stopped(sid)
    assert (await session_execution(sid))[2] is None


@pytest.mark.asyncio
async def test_freeze_while_generation_write_waits_prevents_effect():
    entered, release = asyncio.Event(), asyncio.Event()
    async def before():
        entered.set()
        await release.wait()
    fence = AutomationExecutionFence(before)
    task = asyncio.create_task(fence.enter())
    await entered.wait()
    fence.frozen = True
    release.set()
    with pytest.raises(RuntimeError, match="closed for ownership"):
        await task


from server.codex import schema
from server.tests.test_codex_driver import harness as harness, thread_payload, turn_payload  # noqa: E402


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
async def test_codex_requires_complete_native_background_observation(harness, partial):
    d = await harness.boot()
    await harness.run_turn()
    harness.notify_turn_completed()
    await harness.settle()
    payload = thread_payload()
    payload["turns"] = [turn_payload("turn-1", "completed")]
    harness.server.rule(schema.METHOD_THREAD_READ, result={"thread": payload})
    harness.server.rule(schema.METHOD_THREAD_BACKGROUND_TERMINALS_LIST,
                        result={"data": [], "nextCursor": "unread" if partial else None})
    assert await d.automation_work_stopped() is (not partial)
    if not partial:
        with pytest.raises(RuntimeError, match="closed for ownership"):
            await d.send_prompt("cannot act after transfer")


@pytest.mark.asyncio
async def test_outer_cancellation_does_not_write_native_proof():
    d = driver()
    await d.start()
    await d.send_prompt("hello")
    await d._pump
    started = asyncio.Event()
    async def stuck_close():
        started.set()
        await asyncio.Event().wait()
    d.aclose = stuck_close
    task = asyncio.create_task(d.automation_work_stopped())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not d._automation_close_ok
    # Restore actual cleanup for the test's native stand-in.
    await ClaudeAgentDriver.aclose(d)


@pytest.mark.asyncio
async def test_terminal_close_persists_proof_before_driver_disappears(ownership_db):
    token = await claim([target()])
    async with owned(token) as tx:
        sid = tx.reserve_session()
    d = driver(before_execution=lambda: begin_session_execution(sid))
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    await d.start()
    await d.send_prompt("hello")
    await d._pump
    await orch._close_automation_driver(d, sid)
    association = await session_execution(sid)
    assert association[1] is not None and association[1] == association[2]
    async with owned(token) as tx:
        tx.release(ReleaseProof.WORK_STOPPED, "closed native execution")
    replacement = driver(before_execution=lambda: begin_session_execution(sid))
    with pytest.raises(StaleOwnership):
        await replacement.start()


@pytest.mark.asyncio
async def test_shutdown_timeout_cannot_become_stopped_proof(monkeypatch):
    import server.sessions.claude_driver as module
    monkeypatch.setattr(module, "_CLOSE_AEXIT_TIMEOUT_S", 0.01)
    client = f.FakeSDKClient([f.result()])
    async def never_exits(*args):
        await asyncio.Event().wait()
    client.__aexit__ = never_exits
    d = driver(client)
    await d.start()
    await d.send_prompt("hello")
    await d._pump
    assert not await d.automation_work_stopped()


@pytest.mark.asyncio
async def test_completed_child_proof_survives_parent_turn_and_child_restart_invalidates_tree(ownership_db):
    from server.automations.ownership import execution_snapshot
    from server.db.models import Session
    token = await claim([target()])
    async with owned(token) as tx:
        parent = tx.reserve_session()
    async with ownership_db() as db:
        db.add(Session(id=parent, name="parent", account_name="a", cwd="/tmp"))
        db.add(Session(id="child", parent_session_id=parent, name="child", account_name="a", cwd="/tmp"))
        await db.commit()
    await begin_session_execution(parent)
    await begin_session_execution("child")
    child = await execution_snapshot("child")
    assert child.token == token
    assert await attest_session_stopped(token, child.generation, session_id="child")
    await begin_session_execution(parent)
    root = await execution_snapshot(parent)
    assert await attest_session_stopped(token, root.generation, session_id=parent)
    assert (await session_execution(parent))[1] == (await session_execution(parent))[2]
    await begin_session_execution("child")
    async with owned(token) as tx:
        with pytest.raises(ValueError, match="attestation"):
            tx.release(ReleaseProof.WORK_STOPPED, "child restarted")
    child = await execution_snapshot("child")
    assert await attest_session_stopped(token, child.generation, session_id="child")
    async with owned(token) as tx:
        tx.release(ReleaseProof.WORK_STOPPED, "both executions stopped")
    with pytest.raises(StaleOwnership):
        await begin_session_execution("child")


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [False, True])
async def test_child_lineage_is_available_before_initial_native_execution(tmp_path, queued):
    from server.automations.ownership import acquire, execution_snapshot
    from server.db import db_session
    from server.db.models import Session
    from server.tests.test_assistant_orchestrator import _make_orch, _stop_orch
    orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path / 'child.db'}", tmp_path=tmp_path,
                            max_active_sessions=1 if queued else 20)
    try:
        parent = await orch.create_session(name="parent", cwd=str(tmp_path), identity="test")
        async with acquire([target()], owner="runner", source="test", reason="owned parent") as tx:
            tx.start_action(session_id=parent.id)
            token = tx.token
        entered = []
        original = orch._automation_before_execution
        async def checked(sid, **kwargs):
            await original(sid, **kwargs)
            rec = orch._records.get(sid)
            if rec and rec.parent_session_id == parent.id:
                snap = await execution_snapshot(sid, parent_session_id=parent.id)
                assert snap.token == token and snap.generation is not None
                async with db_session() as db:
                    entered.append((sid, await db.get(Session, sid) is not None))
        orch._automation_before_execution = checked
        child = await orch.dispatch_child_session(parent.id, name="child", prompt="hello", model=None,
                                                 permission_mode=None, identity="test")
        async with db_session() as db:
            row = await db.get(Session, child["id"])
            assert row.parent_session_id == parent.id
        if queued:
            assert child["status"] == "queued" and not entered
            async with db_session() as db:
                from server.db.models import SessionQueue
                queue = await db.get(SessionQueue, child["id"])
                assert queue.params["parent_session_id"] == parent.id
            orch._max_active_sessions = AsyncMock(return_value=20)
            await orch._maybe_start_queued()
            assert entered
        else:
            assert entered and entered[0] == (child["id"], False)
    finally:
        await _stop_orch(orch)


@pytest.mark.asyncio
@pytest.mark.parametrize("child_active", [False, True])
async def test_codex_recursively_observes_completed_collaborators_and_cycles(child_active):
    from server.codex.background import managed_thread_tree_stopped
    def thread(sid, receiver, active=False):
        item = schema.CollabAgentToolCallItem("call", "spawnAgent", "completed", sid,
                                             (receiver,), {})
        turn = SimpleNamespace(status=schema.TurnStatus.COMPLETED, items=[item])
        return SimpleNamespace(thread=SimpleNamespace(id=sid, turns=[turn],
            status=SimpleNamespace(type="active" if active else "idle")))
    responses = {"parent": thread("parent", "child"),
                 "child": thread("child", "parent", child_active)}
    client = SimpleNamespace(
        read_thread=AsyncMock(side_effect=lambda params: responses[params.thread_id]),
        list_background_terminals=AsyncMock(return_value=SimpleNamespace(data=[], next_cursor=None)))
    assert await managed_thread_tree_stopped(client, "parent") is (not child_active)
    assert client.read_thread.await_count == 2  # cycle terminates, no omission of child


@pytest.mark.asyncio
async def test_cancelled_registered_child_reconciles_without_inventing_native_stop(ownership_db):
    from server.automations.ownership import execution_snapshot, register_child_execution
    from server.db.models import Session
    token = await claim([target()])
    async with owned(token) as tx:
        parent = tx.reserve_session()
    await begin_session_execution(parent)
    await register_child_execution("queued", parent)
    async with ownership_db() as db:
        db.add(Session(id=parent, name="parent", account_name="a", cwd="/tmp", status="ended"))
        db.add(Session(id="queued", parent_session_id=parent, name="queued", account_name="a",
                       cwd="/tmp", status="queued"))
        await db.commit()
    root = await execution_snapshot(parent)
    assert await attest_session_stopped(token, root.generation, session_id=parent)
    assert (await session_execution(parent))[2] is None
    async with ownership_db() as db:
        row = await db.get(Session, "queued")
        row.status = "ended"
        await db.commit()
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {}
    orch._drivers = {}
    assert await orch._automation_session_work_stopped(parent)
    async with owned(token) as tx:
        tx.release(ReleaseProof.WORK_STOPPED, "root closed; child cancelled before native entry")
    with pytest.raises(StaleOwnership):
        await begin_session_execution("queued")


@pytest.mark.asyncio
async def test_new_driver_cannot_erase_unproven_predecessor_lifetime(ownership_db):
    from server.automations.ownership import execution_snapshot
    token = await claim([target()])
    async with owned(token) as tx:
        sid = tx.reserve_session()
    await begin_session_execution(sid, execution_id="old-driver")
    old = await execution_snapshot(sid, execution_id="old-driver")
    await begin_session_execution(sid, execution_id="replacement")
    new = await execution_snapshot(sid, execution_id="replacement")
    assert await attest_session_stopped(token, new.generation, session_id=sid, execution_id="replacement")
    async with owned(token) as tx:
        with pytest.raises(ValueError, match="attestation"):
            tx.release(ReleaseProof.WORK_STOPPED, "old shutdown was unknown")
    assert await attest_session_stopped(token, old.generation, session_id=sid, execution_id="old-driver")
    async with owned(token) as tx:
        tx.release(ReleaseProof.WORK_STOPPED, "both driver lifetimes actually stopped")


@pytest.mark.asyncio
async def test_retained_child_association_survives_ancestor_row_deletion(ownership_db):
    from server.db.models import Session
    token = await claim([target()])
    async with owned(token) as tx:
        parent = tx.reserve_session()
    async with ownership_db() as db:
        db.add(Session(id="child", parent_session_id=parent, name="child", account_name="a", cwd="/tmp"))
        db.add(Session(id="grandchild", parent_session_id="child", name="grandchild", account_name="a", cwd="/tmp"))
        await db.commit()
    await begin_session_execution("grandchild")
    async with ownership_db() as db:
        await db.delete(await db.get(Session, "child"))
        await db.commit()
    # Durable operation membership wins even though the lineage now has a gap.
    assert (await session_execution("grandchild"))[0] == token


@pytest.mark.asyncio
async def test_nonterminal_clean_close_proof_allows_safe_managed_resume(ownership_db):
    from server.automations.ownership import execution_snapshot
    token = await claim([target()])
    async with owned(token) as tx:
        sid = tx.reserve_session()
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {sid: SimpleNamespace(parent_session_id=None, status="done")}
    await orch._automation_before_execution(sid, execution_id="old")
    old = await execution_snapshot(sid, execution_id="old")
    await attest_session_stopped(token, old.generation, session_id=sid, execution_id="old", allow_tree_stop=False)
    assert not await orch._automation_session_work_stopped(sid)  # warm workflow is not complete
    assert await orch._automation_before_execution(sid, execution_id="resumed")
    d = driver(resume="historical", before_execution=AsyncMock(return_value=True))
    # Avoid irrelevant transcript repair work in this focused native contract test.
    d.resume = None
    await d.start()
    assert d._automation_observation_complete
    await d.aclose()


@pytest.mark.asyncio
async def test_failed_claude_clear_remains_unknown_after_clean_replacement_close():
    old = f.FakeSDKClient([f.result()])
    old.__aexit__ = AsyncMock(side_effect=RuntimeError("old shutdown failed"))
    d = driver(old)
    await d.start()
    await d.send_prompt("old")
    await d._pump
    replacement = f.FakeSDKClient([f.result()])
    d._factory = lambda *a, **k: replacement
    await d.reconnect()
    await d.send_prompt("new")
    await d._pump
    await d.aclose()
    assert d._automation_close_ok
    assert not await d.automation_work_stopped()


@pytest.mark.asyncio
async def test_archived_session_with_no_driver_attests_every_retained_execution(ownership_db):
    # #2009: mark_complete released the driver mid-turn (the probe saw a live turn
    # and refused), or the session resumed past a drain-cap kill (predecessor
    # unknown, so the probe can never pass). Either way the row was then archived
    # with executions still unstopped, and a driverless archived session had no
    # path left to proof — its claim held the product slot forever (KinDash at
    # 1/1 behind session d97a69cb, 2026-09-19).
    from server.db import db_session, models
    token = await claim([target()])
    async with owned(token) as tx:
        sid = tx.reserve_session()
    await begin_session_execution(sid, execution_id="gen-a")
    await begin_session_execution(sid, execution_id="gen-b")   # a resume: two retained lifetimes
    async with db_session() as db:
        db.add(models.Session(id=sid, name="runner", account_name="acct", cwd="/tmp",
                              status="archived"))
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {}
    orch._drivers = {}
    orch._releasing = {}
    orch._enabled_host_names = AsyncMock(return_value=frozenset())
    assert await orch._automation_session_work_stopped(sid)
    _token, tree_generation, tree_stopped = await session_execution(sid)
    assert tree_generation == tree_stopped == 2


@pytest.mark.asyncio
async def test_archived_session_falls_through_to_terminal_sweep_when_shortcut_alone_cannot_converge(
        ownership_db):
    """cairnea/KinDash#16529 (2026-09-21): the session's OWN current execution
    closed cleanly and was individually attested before archiving, but an
    EARLIER resume's predecessor execution never was (killed by a restart).
    The cheap "my own generation already matches" shortcut fired, tried to
    reconcile, and failed — the stale sibling still blocks the tree-wide
    aggregate — and used to return False right there, never trying the
    terminal-attestation sweep below it, which for an archived, driverless,
    owned row is allowed to close every retained execution and WOULD have
    converged (the same bar ``test_archived_session_with_no_driver_attests_
    every_retained_execution`` already trusts when NEITHER execution was ever
    individually attested)."""
    from server.automations.ownership import execution_snapshot
    from server.db import db_session, models
    token = await claim([target()])
    async with owned(token) as tx:
        sid = tx.reserve_session()
    await begin_session_execution(sid, execution_id="gen-a")  # killed, never attested
    await begin_session_execution(sid, execution_id="gen-b")  # resumed, closes cleanly
    current = await execution_snapshot(sid, execution_id="gen-b")
    assert await attest_session_stopped(token, current.generation, session_id=sid,
                                        execution_id="gen-b")
    async with db_session() as db:
        db.add(models.Session(id=sid, name="runner", account_name="acct", cwd="/tmp",
                              status="archived"))
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {}
    orch._drivers = {}
    orch._releasing = {}
    orch._enabled_host_names = AsyncMock(return_value=frozenset())
    assert await orch._automation_session_work_stopped(sid)
    _token, tree_generation, tree_stopped = await session_execution(sid)
    assert tree_generation == tree_stopped == 2


@pytest.mark.asyncio
async def test_archived_session_still_releasing_or_owned_elsewhere_is_not_attested(ownership_db):
    from server.db import db_session, models
    token = await claim([target()])
    async with owned(token) as tx:
        sid = tx.reserve_session()
    await begin_session_execution(sid)
    async with db_session() as db:
        db.add(models.Session(id=sid, name="runner", account_name="acct", cwd="/tmp",
                              status="archived", host="elsewhere"))
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {}
    orch._drivers = {}
    orch._releasing = {}
    # Stamped for a live worker: its driver is not visible here, so no proof.
    orch._enabled_host_names = AsyncMock(return_value=frozenset({"elsewhere"}))
    assert not await orch._automation_session_work_stopped(sid)
    assert (await session_execution(sid))[2] is None
    # Own row, but the completion release is still closing the driver: not over yet.
    orch._enabled_host_names = AsyncMock(return_value=None)
    async with db_session() as db:
        row = await db.get(models.Session, sid)
        row.host = None
    pending = asyncio.get_running_loop().create_future()
    orch._releasing = {sid: pending}
    assert not await orch._automation_session_work_stopped(sid)
    pending.cancel()
    assert (await session_execution(sid))[2] is None
