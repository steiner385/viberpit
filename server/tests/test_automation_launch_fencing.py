"""Durable launch CAS at the actual session creation boundary."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from server.automations.ownership import CanonicalTarget, acquire, owned
from server.db import db_session, dispose_db, init_db
from server.db.models import Session
from server.sessions.engine import AgentEngine
from server.sessions.orchestrator import SessionOrchestrator


@pytest_asyncio.fixture
async def launch(tmp_path, monkeypatch):
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path}/launch.db")
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {}
    orch._engine_registry = SimpleNamespace(require=lambda engine: engine)
    orch._active_session_count = lambda: 0
    orch._max_active_sessions = AsyncMock(return_value=10)
    orch._effective_repo_root = AsyncMock(return_value=str(tmp_path))
    monkeypatch.setattr("server.sessions.model_policy.resolve_model_policy", AsyncMock(return_value="allow"))
    clone = Mock(return_value=str(tmp_path))
    monkeypatch.setattr("server.sessions.orchestrator.gitrepos.ensure_clone", clone)

    async def persist(session_id, **kwargs):
        async with db_session() as db:
            row = Session(id=session_id, name=kwargs["name"], account_name="test",
                          cwd=str(tmp_path), status="idle", origin=kwargs["origin"],
                          origin_ref=kwargs["origin_ref"])
            db.add(row)
            await db.flush()
            return orch._record_from_row(row)

    orch._spawn_session = AsyncMock(side_effect=persist)
    orch._hold_for_approval = AsyncMock(side_effect=persist)
    orch._enqueue_session = AsyncMock(side_effect=persist)
    async with acquire([CanonicalTarget("I_test", "o/r#1")], owner="backlog-runner",
                       source="automatic", reason="dispatch",
                       provenance={"origin_ref": "o/r#1"}) as tx:
        token = tx.token
    yield orch, token, clone
    await dispose_db()


def args(token):
    return dict(name="test", repo="o/r", origin="backlog-runner",
                origin_ref="o/r#1", ownership_token=token)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["spawn", "hold", "queue"])
async def test_reserved_id_is_shared_by_all_creation_paths_and_retry(launch, monkeypatch, path):
    orch, token, clone = launch
    if path == "hold":
        monkeypatch.setattr("server.sessions.model_policy.resolve_model_policy", AsyncMock(return_value="require_approval"))
    elif path == "queue":
        orch._max_active_sessions.return_value = 0
    first = await orch.create_session(**args(token))
    # Simulate a retry on another process: no in-memory session may be required.
    orch._records.clear()
    second = await orch.create_session(**args(token))
    assert first.id == second.id
    assert clone.call_count == 1
    assert sum(f.await_count for f in (orch._spawn_session, orch._hold_for_approval,
                                      orch._enqueue_session)) == 1
    async with owned(token) as tx:
        assert tx.operation.reserved_session_id == first.id
        assert tx.operation.session_id == first.id
        assert tx.operation.phase == "active"


@pytest.mark.asyncio
async def test_concurrent_retry_cannot_clone_or_launch_twice(launch):
    orch, token, clone = launch
    entered, finish = asyncio.Event(), asyncio.Event()
    persist = orch._spawn_session.side_effect

    async def wait_then_persist(*a, **kw):
        entered.set()
        await finish.wait()
        return await persist(*a, **kw)
    orch._spawn_session.side_effect = wait_then_persist
    first = asyncio.create_task(orch.create_session(**args(token)))
    await entered.wait()
    try:
        with pytest.raises(RuntimeError, match="uncertain"):
            await orch.create_session(**args(token))
        assert clone.call_count == 1
        assert orch._spawn_session.await_count == 1
    finally:
        finish.set()
        await first


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("driver failed"), asyncio.CancelledError()])
async def test_failure_or_cancellation_retains_claim_and_blocks_retry(launch, failure):
    orch, token, clone = launch
    orch._spawn_session.side_effect = failure
    with pytest.raises(type(failure)):
        await orch.create_session(**args(token))
    async with owned(token) as tx:
        assert tx.operation.phase == "uncertain"
        assert tx.operation.reserved_session_id
    with pytest.raises(RuntimeError, match="uncertain"):
        await orch.create_session(**args(token))
    assert clone.call_count == 1


@pytest.mark.asyncio
async def test_unknown_token_never_clones(launch):
    orch, _, clone = launch
    with pytest.raises(RuntimeError):
        await orch.create_session(**args("not-a-token"))
    clone.assert_not_called()
    orch._spawn_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_engine_refusal_precedes_reservation(launch):
    orch, token, clone = launch
    orch._engine_registry.require = Mock(side_effect=ValueError("disabled engine"))
    with pytest.raises(ValueError, match="disabled engine"):
        await orch.create_session(**args(token))
    async with owned(token) as tx:
        assert tx.operation.phase == "claimed"
    clone.assert_not_called()


@pytest.mark.asyncio
async def test_context_claimed_launch_ignores_origin_dedupe(launch):
    from server.automations.context import AutomationContext
    orch, token, _ = launch
    ctx = AutomationContext("backlog-runner", create_session=orch.create_session)
    ctx._existing_session = AsyncMock(return_value=SimpleNamespace(id="wrong-session"))
    ctx.launch_plan_or_refuse = AsyncMock(return_value=(AgentEngine.CLAUDE_CODE, {}))
    ctx.emit = AsyncMock()
    rec = await ctx.spawn_session(origin_ref="o/r#1", ownership_token=token, name="test", repo="o/r")
    assert rec.id != "wrong-session"
    ctx._existing_session.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"origin": "pr-doctor"}, {"origin_ref": "o/r#2"}])
async def test_other_origin_or_target_cannot_use_token(launch, change):
    orch, token, clone = launch
    with pytest.raises(ValueError, match="ownership does not match"):
        await orch.create_session(**(args(token) | change))
    async with owned(token) as tx:
        assert tx.operation.phase == "claimed"
    clone.assert_not_called()


@pytest.mark.asyncio
async def test_persisted_session_after_lost_response_is_joined_without_relaunch(launch):
    orch, token, clone = launch
    persist = orch._spawn_session.side_effect

    async def persist_then_fail(*a, **kw):
        await persist(*a, **kw)
        raise RuntimeError("response lost")
    orch._spawn_session.side_effect = persist_then_fail
    with pytest.raises(RuntimeError, match="response lost"):
        await orch.create_session(**args(token))
    rec = await orch.create_session(**args(token))
    async with owned(token) as tx:
        assert rec.id == tx.operation.reserved_session_id
        # Joining does not assert that unknown native work has stopped.
        assert tx.operation.phase == "uncertain"
    assert clone.call_count == 1
    orch._spawn_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_hard_model_refusal_leaves_claim_unstarted(launch, monkeypatch):
    from server.sessions.orchestrator import ModelBlockedError
    orch, token, clone = launch
    monkeypatch.setattr("server.sessions.model_policy.resolve_model_policy", AsyncMock(return_value="block"))
    with pytest.raises(ModelBlockedError):
        await orch.create_session(**args(token))
    async with owned(token) as tx:
        assert tx.operation.phase == "claimed"
    clone.assert_not_called()


@pytest.mark.asyncio
async def test_internal_token_survives_real_rpc_roundtrip(launch):
    from server.ipc.client import DaemonClient
    from server.ipc.daemon import create_daemon_app
    from server.ipc.orchestrator_rpc import register_orchestrator_rpc
    from server.sessions.remote import RemoteOrchestrator
    from server.tests.ipc_harness import serve_daemon_uds

    orch, token, clone = launch
    app = create_daemon_app()
    register_orchestrator_rpc(app, orch)
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            remote = RemoteOrchestrator(client)
            first = await remote.create_session(**args(token))
            second = await remote.create_session(**args(token))
        finally:
            await client.aclose()
    assert first.id == second.id
    assert clone.call_count == 1


def test_public_create_request_cannot_supply_ownership_capability():
    from server.api.sessions import CreateSessionRequest
    request = CreateSessionRequest.model_validate({"ownership_token": "bad", "session_id": "bad"})
    assert "ownership_token" not in request.model_dump()
    assert "session_id" not in request.model_dump()


@pytest.mark.asyncio
@pytest.mark.parametrize("persisted_host", ["worker-a", None])
async def test_fleet_join_does_not_reassign_host(persisted_host):
    from server.sessions.fleet_orchestrator import FleetOrchestrator
    rec = SimpleNamespace(id="reserved", host=persisted_host)
    remote = SimpleNamespace(create_session=AsyncMock(return_value=rec))
    router = SimpleNamespace(pick=AsyncMock(return_value=["worker-b"]), get=lambda host: remote)
    fleet = FleetOrchestrator(router, primary="primary")
    fleet._stamp_host = AsyncMock()
    result = await fleet.create_session(name="test", ownership_token="internal-token")
    assert result.host == persisted_host
    assert fleet._host_cache["reserved"] == (persisted_host or "primary")
    fleet._stamp_host.assert_not_awaited()


@pytest.mark.asyncio
async def test_caller_cancellation_is_not_swallowed(launch):
    orch, token, _ = launch
    entered = asyncio.Event()

    async def wait_forever(*a, **kw):
        entered.set()
        await asyncio.Future()
    orch._spawn_session.side_effect = wait_forever
    task = asyncio.create_task(orch.create_session(**args(token)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with owned(token) as tx:
        assert tx.operation.phase == "uncertain"
    with pytest.raises(RuntimeError, match="uncertain"):
        await orch.create_session(**args(token))


@pytest.mark.asyncio
async def test_claim_database_failure_does_not_launch(launch, monkeypatch):
    orch, token, clone = launch
    monkeypatch.setattr("server.automations.ownership.owned", Mock(side_effect=RuntimeError("database down")))
    with pytest.raises(RuntimeError, match="database down"):
        await orch.create_session(**args(token))
    clone.assert_not_called()
    orch._spawn_session.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("wait", [True, False])
async def test_wait_for_slot_intent_queues_the_launch_as_runner_capacity(launch, wait):
    """A manual dispatch admitted only to WAIT for a slot (its claim's intent says
    ``wait_for_slot``) is persisted queued, not spawned — under a cap with room."""
    orch, token, _clone = launch
    async with owned(token) as tx:
        tx.operation.provenance = {**tx.operation.provenance, "runner_dispatch": {
            "expected": {"repo": "o/r"}, "source": "manual",
            **({"wait_for_slot": True} if wait else {})}}
    await orch.create_session(**args(token))
    if wait:
        orch._enqueue_session.assert_awaited_once()
        assert orch._enqueue_session.await_args.kwargs["queue_reason"] == "runner_capacity"
        orch._spawn_session.assert_not_awaited()
    else:
        orch._spawn_session.assert_awaited_once()
        orch._enqueue_session.assert_not_awaited()
