"""Fleet mirrors must not become local recovery or autopilot actors (#1738)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from server.automations.autopilot_policy import Decision
from server.db import db_session, init_db
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SYSTEM_IDENTITY
from server.tests.test_host_partitioned_ownership import _record


@pytest.fixture
def actor(monkeypatch):
    import server.sessions.orchestrator as mod

    monkeypatch.setattr(mod, "daemon_role", lambda: "primary")
    monkeypatch.setattr(mod, "self_host_name", lambda: "dobby")
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {}
    orch._drivers = {}
    orch._turn_restart_at = {}
    orch._enabled_host_names = AsyncMock(return_value=frozenset({"dobby", "rocky"}))
    orch._autopilot_read_config = AsyncMock(return_value=("autonomous", {}))
    orch._autopilot_mark_assisted = AsyncMock()
    orch.ensure_driver = AsyncMock(return_value=object())
    orch.send_prompt = AsyncMock(return_value="sent")
    return orch


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [frozenset({"dobby", "rocky"}), None])
async def test_autopilot_does_not_select_foreign_mirrors(actor, enabled):
    actor._enabled_host_names.return_value = enabled
    actor._records = {sid: _record(sid, host, "waiting_input")
                      for sid, host in (("local", "dobby"), ("foreign", "rocky"))}
    assert await actor._autopilot_list_governed() == ["local"]


@pytest.mark.asyncio
@pytest.mark.parametrize("host,enabled,expected", [
    ("rocky", frozenset({"rocky"}), False),
    ("rocky", None, False),
    ("dobby", None, True),
    (None, None, True),
    ("gone", frozenset({"rocky"}), True),
])
async def test_autopilot_rechecks_actor_before_nudge(actor, host, enabled, expected):
    actor._records["s1"] = _record("s1", host, "waiting_input")
    actor._enabled_host_names.return_value = enabled
    assert await actor._autopilot_apply("s1", Decision(action="nudge", prompt="continue")) is expected
    assert actor.ensure_driver.await_count == int(expected)
    assert actor.send_prompt.await_count == int(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["nudge", "approve", "deny", "answer"])
async def test_autopilot_does_not_send_after_lease_refusal(actor, action):
    actor._records["s1"] = _record("s1", "dobby", "waiting_input")
    actor.ensure_driver.return_value = None
    actor.resolve_permission = Mock()
    actor.answer_question = AsyncMock()
    ctx = SimpleNamespace(stuck={"perm_id": "p1", "tool_name": "Bash",
                                "tool_input": {"command": "pytest"}, "question_id": "q1"})
    assert await actor._autopilot_apply(
        "s1", Decision(action=action, prompt="continue"), ctx) is False
    actor.send_prompt.assert_not_awaited()
    actor.resolve_permission.assert_not_called()
    actor.answer_question.assert_not_awaited()
    actor._autopilot_mark_assisted.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_resume_only_owned_interrupted_records(actor, monkeypatch):
    monkeypatch.setattr(SessionOrchestrator, "_env_truthy", staticmethod(lambda _: True))
    actor._records = {sid: _record(sid, host, "idle")
                      for sid, host in (("local", "dobby"), ("foreign", "rocky"))}
    for rec in actor._records.values():
        rec.interrupted_by_restart = True
        rec.interrupted_from_status = "working"
    actor.resume_interrupted = AsyncMock(return_value="resumed")
    assert await actor.auto_resume_interrupted() == 1
    actor.resume_interrupted.assert_awaited_once_with("local", SYSTEM_IDENTITY)


@pytest.mark.asyncio
async def test_startup_rearm_leaves_foreign_snapshot_for_owner(actor, tmp_path):
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'rearm.db'}")
    actor._records = {sid: _record(sid, host, "idle")
                      for sid, host in (("local", "dobby"), ("foreign", "rocky"))}
    async with db_session() as db:
        for sid, rec in actor._records.items():
            db.add(DbSession(id=sid, name=sid, account_name="acct", status="idle",
                             cwd="/tmp", host=rec.host, pending_prompt="continue"))
    actor._rearmed = set()
    actor._rearm_session = AsyncMock(return_value=True)
    actor._startup_note = lambda *args: None
    actor.startup_report = lambda: {}
    assert await actor.rearm_background_tasks() == 1
    actor._rearm_session.assert_awaited_once_with("local", None, "continue")
    async with db_session() as db:
        foreign = await db.get(DbSession, "foreign")
        assert foreign.pending_prompt == "continue"


@pytest.mark.asyncio
async def test_autopilot_keeps_adopted_live_driver(actor):
    actor._records["s1"] = _record("s1", "rocky", "waiting_input")
    actor._drivers["s1"] = object()
    assert await actor._autopilot_list_governed() == ["s1"]
    assert await actor._autopilot_apply("s1", Decision(action="nudge", prompt="continue"))


@pytest.mark.asyncio
async def test_auto_resume_worker_owns_only_its_stamp(actor, monkeypatch):
    import server.sessions.orchestrator as mod

    monkeypatch.setattr(mod, "daemon_role", lambda: "worker")
    monkeypatch.setattr(mod, "self_host_name", lambda: "rocky")
    monkeypatch.setattr(SessionOrchestrator, "_env_truthy", staticmethod(lambda _: True))
    actor._records = {sid: _record(sid, host, "idle") for sid, host in
                      (("mine", "rocky"), ("primary", "dobby"), ("legacy", None))}
    for rec in actor._records.values():
        rec.interrupted_by_restart = True
        rec.interrupted_from_status = "working"
    actor.resume_interrupted = AsyncMock(return_value="resumed")
    assert await actor.auto_resume_interrupted() == 1
    actor.resume_interrupted.assert_awaited_once_with("mine", SYSTEM_IDENTITY)


@pytest.mark.asyncio
async def test_autopilot_rechecks_restart_after_registry_await(actor):
    actor._records["s1"] = _record("s1", "dobby", "waiting_input")
    actor._clock = lambda: 100.0

    async def hosts_during_swap():
        actor._turn_restart_at["s1"] = 100.0
        return frozenset({"dobby", "rocky"})

    actor._enabled_host_names.side_effect = hosts_during_swap
    assert await actor._autopilot_apply("s1", Decision(action="nudge", prompt="continue")) is False
    actor.ensure_driver.assert_not_awaited()
    actor.send_prompt.assert_not_awaited()
