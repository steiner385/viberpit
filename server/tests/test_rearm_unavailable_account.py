# server/tests/test_rearm_unavailable_account.py
"""A session bound to a removed account must not WARN on every boot (#3436).

The rearm pass used to reach ``configdir.compose`` and surface its raw
``FileNotFoundError`` at WARNING — auto-filing an issue on every daemon start —
and a compose failure after the lease claim left the resume lease held.
"""
import asyncio
import logging

import pytest
from sqlalchemy import select

from server.db import db_session
from server.db.models import Session as DbSession
from server.sessions import orchestrator as orch_mod
from server.sessions import session_lease
from server.sessions.engine import AgentEngine
from server.sessions.orchestrator import AccountUnavailable, SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

_SNAP = [{"tool_use_id": "toolu_mon", "kind": "monitor", "name": "Monitor",
          "input": {"description": "Watch PR", "command": "z"}, "description": "Watch PR"}]


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("VIBERPIT_HOME", str(h))
    return h


async def _assert_lease_released(orch, sid: str) -> None:
    state = await session_lease.current(sid)
    assert state is not None, "the resume should have claimed the lease"
    assert state.age_s > orch_mod.SESSION_LEASE_TTL_S, "lease still held"
    assert sid not in orch._lease_fences()
    assert sid not in orch._drivers


async def _orch_with(tmp_path, **cols) -> SessionOrchestrator:
    async with db_session() as db:
        db.add(DbSession(id="s1", name="n", account_name="gone", cwd=str(tmp_path), **cols))
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    await orch.rehydrate()
    return orch


def test_rearm_of_removed_account_is_info_and_retains_snapshot(
        tmp_path, home, isolated_db, caplog):
    async def run():
        orch = await _orch_with(tmp_path, pending_rearm=_SNAP, pending_prompt="resume me")
        caplog.set_level(logging.INFO, logger="server.sessions.orchestrator")

        assert await orch.rearm_background_tasks() == 0

        warnings = [r for r in caplog.records
                    if r.name == "server.sessions.orchestrator"
                    and r.levelno >= logging.WARNING]
        assert warnings == []
        assert any("snapshot retained" in r.getMessage() and "not in the account manifest"
                   in r.getMessage() for r in caplog.records)
        async with db_session() as db:
            row = (await db.execute(select(DbSession).where(DbSession.id == "s1"))).scalar_one()
        assert row.pending_rearm == _SNAP and row.pending_prompt == "resume me"
        assert "s1" not in orch._rearmed
        await _assert_lease_released(orch, "s1")

    asyncio.run(run())


def test_compose_failure_for_a_present_account_still_raises_raw(
        tmp_path, home, isolated_db, monkeypatch):
    """Only an unresolvable account is translated; any other compose fault (a torn
    sshfs read) surfaces unchanged — and the lease is still released."""
    async def run():
        orch = await _orch_with(tmp_path)
        monkeypatch.setattr(orch_mod, "_account_unavailable_reason", lambda *_: None)

        def _boom(_account):
            raise FileNotFoundError("torn read")
        monkeypatch.setattr(orch_mod, "compose", _boom)

        with pytest.raises(FileNotFoundError):
            await orch.ensure_driver("s1")
        await _assert_lease_released(orch, "s1")

    asyncio.run(run())


def test_ensure_driver_for_removed_account_raises_typed_and_releases_lease(
        tmp_path, home, isolated_db):
    async def run():
        orch = await _orch_with(tmp_path)
        with pytest.raises(AccountUnavailable) as ei:
            await orch.ensure_driver("s1")
        assert ei.value.account == "gone"
        assert ei.value.reason == "not in the account manifest"
        assert ei.value.__cause__ is not None   # compose()'s original fault is chained
        await _assert_lease_released(orch, "s1")

    asyncio.run(run())


def test_account_unavailable_reason(home):
    from viberpit import account_registry, paths
    from viberpit.creds import write_json_secure

    assert orch_mod._account_unavailable_reason(AgentEngine.CODEX, "gone") is None
    assert (orch_mod._account_unavailable_reason(AgentEngine.CLAUDE_CODE, "gone")
            == "not in the account manifest")

    account_registry.register_account("present", "anthropic")
    assert (orch_mod._account_unavailable_reason(AgentEngine.CLAUDE_CODE, "present")
            == "credential slot missing")
    write_json_secure(paths.slot_path("present"), {"claudeAiOauth": {}})
    assert orch_mod._account_unavailable_reason(AgentEngine.CLAUDE_CODE, "present") is None


def test_account_unavailable_message_names_account():
    exc = AccountUnavailable("gone", "not in the account manifest")
    assert "'gone'" in str(exc) and "not in the account manifest" in str(exc)
    assert orch_mod._resume_failure_level(exc) == logging.INFO
    assert orch_mod._resume_failure_level(FileNotFoundError()) == logging.WARNING
