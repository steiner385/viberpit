"""disabled_for_session + the orchestrator wiring that writes it at every Claude driver build."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from server.db.models import Session as DbSession
from server.db import db_session
from server.mcp_scope import apply, store
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory
from server.tests.test_create_session_model import _fake_usage, _seed_account, _spawn_env


@pytest.fixture
def servers(monkeypatch):
    monkeypatch.setattr(apply, "configured_server_keys", lambda: ["gh", "plugin:p:s", "other"])


@pytest.mark.asyncio
async def test_resume_applies_current_rules_under_session_override(isolated_db, servers):
    async with db_session() as db:
        db.add(DbSession(id="s1", name="n", account_name="acct", cwd="/tmp"))
    await store.set_default("gh", False)
    await store.set_repo_rule("a/b", "gh", True)
    await store.set_session_override("s1", "plugin:p:s", False)
    assert await apply.disabled_for_session(repo="a/b", session_id="s1") == ["plugin:p:s"]
    # The rule changes between builds: the next build sees the new rule.
    await store.set_repo_rule("a/b", "gh", False)
    assert await apply.disabled_for_session(repo="a/b", session_id="s1") == ["gh", "plugin:p:s"]


@pytest.mark.asyncio
async def test_scratch_session_ignores_repo_rules(isolated_db, servers):
    await store.set_repo_rule("a/b", "gh", False)
    assert await apply.disabled_for_session(repo=None, session_id=None) == []


@pytest.mark.asyncio
async def test_kill_switch_off_returns_none(isolated_db, servers, monkeypatch):
    await store.set_default("gh", False)
    monkeypatch.setattr(apply, "get_settings", lambda: SimpleNamespace(mcp_scope_enabled=False))
    assert await apply.disabled_for_session(repo="a/b", session_id=None) is None


@pytest.mark.asyncio
async def test_store_failure_returns_none_without_warning(isolated_db, servers, monkeypatch, caplog):
    async def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(store, "server_defaults", boom)
    with caplog.at_level(logging.DEBUG):
        assert await apply.disabled_for_session(repo="a/b", session_id=None) is None
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_unreadable_server_list_returns_none(isolated_db, monkeypatch):
    def boom():
        raise OSError("no config")
    monkeypatch.setattr(apply, "configured_server_keys", boom)
    assert await apply.disabled_for_session(repo=None, session_id=None) is None


def test_spawn_and_ensure_driver_both_write_the_list(tmp_path):
    from server.db import init_db
    db_url, repo_root, (env_patch, compose_patch, trust_patch, select_patch) = _spawn_env(tmp_path)
    fake = AsyncMock(return_value=["gh"])
    with env_patch, compose_patch, trust_patch as trust, select_patch, \
            patch("server.sessions.orchestrator.disabled_for_session", fake):
        _seed_account("alpha")

        async def _run():
            await init_db(db_url=db_url)
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                                       usage_fn=_fake_usage, repo_root=str(repo_root))
            await orch.start()
            try:
                rec = await orch.create_session("auto")
                assert rec.id in orch._drivers
                spawn_calls = list(trust.call_args_list)
                orch._drivers.pop(rec.id)  # simulate a detached/rehydrated session
                trust.reset_mock()
                await orch.ensure_driver(rec.id)
                return rec.id, spawn_calls, list(trust.call_args_list)
            finally:
                await orch.stop()

        sid, spawn_calls, resume_calls = asyncio.run(_run())
    assert spawn_calls and spawn_calls[-1].kwargs["disabled_mcp"] == ["gh"]
    assert resume_calls and resume_calls[-1].kwargs["disabled_mcp"] == ["gh"]
    # spawn has no session row yet (no overrides); resume reads the row's own id
    assert fake.await_args_list[0].kwargs["session_id"] is None
    assert fake.await_args_list[-1].kwargs["session_id"] == sid


def test_setting_defaults_on_and_reads_env(monkeypatch):
    from server.config import Settings
    monkeypatch.delenv("VIBERPIT_MCP_SCOPE_ENABLED", raising=False)
    assert Settings().mcp_scope_enabled is True
    monkeypatch.setenv("VIBERPIT_MCP_SCOPE_ENABLED", "false")
    assert Settings().mcp_scope_enabled is False
