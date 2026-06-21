"""Tests for Task 4: ensure_driver lazily resumes a detached session on attach."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.db import init_db, db_session
from server.db.models import Session as DbSession
from server.sessions.driver import SessionDriver
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

def _seed_and_rehydrate(db_url: str, session_id: str, cwd: str,
                         claude_session_id: str | None, account: str = "acct1"):
    """Seed a DB row and rehydrate an orchestrator from it, return the orchestrator."""
    async def _run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(
                id=session_id,
                name="test-session",
                account_name=account,
                claude_session_id=claude_session_id,
                status="idle",
                cwd=cwd,
            ))
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        await orch.rehydrate()
        return orch

    return asyncio.run(_run())


# -----------------------------------------------------------------------
# Test 1: ensure_driver resumes a detached session with claude_session_id
# -----------------------------------------------------------------------

def test_ensure_driver_resumes_detached_session(tmp_path):
    """ensure_driver returns a driver for a detached row, calling sdk with resume=<claude_session_id>."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    cwd = str(tmp_path)
    session_id = "s1"
    claude_resume_id = "claude-resume-me"

    # Track what resume value the factory was called with
    resume_calls = []

    def tracking_factory(sdk_cwd, cfg_dir, can_use_tool, mode, resume):
        resume_calls.append(resume)
        from server.tests.fakes.fake_sdk import FakeSDKClient
        return FakeSDKClient([], on_can_use_tool=can_use_tool)

    async def _run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(
                id=session_id, name="test", account_name="acct1",
                claude_session_id=claude_resume_id, status="idle", cwd=cwd,
            ))
        orch = SessionOrchestrator(sdk_factory=tracking_factory)
        await orch.rehydrate()

        cfg_dir = tmp_path / "cfg" / "acct1"
        cfg_dir.mkdir(parents=True)

        with patch("server.sessions.orchestrator.compose", return_value=cfg_dir), \
             patch("server.sessions.orchestrator.configdir") as mock_configdir:
            driver = await orch.ensure_driver(session_id)

        return orch, driver, resume_calls

    orch, driver, resume_calls = asyncio.run(_run())

    assert driver is not None, "expected a driver to be returned"
    assert resume_calls == [claude_resume_id], (
        f"expected factory called with resume={claude_resume_id!r}, got {resume_calls}"
    )
    # Driver is now registered
    assert orch.driver(session_id) is driver
    # Record is marked not-detached
    rec = orch.get_session(session_id)
    assert rec is not None
    assert rec.detached is False


# -----------------------------------------------------------------------
# Test 2: ensure_driver handles claude_session_id=None (fresh start)
# -----------------------------------------------------------------------

def test_ensure_driver_empty_claude_session_id(tmp_path):
    """ensure_driver with no claude_session_id calls factory with resume=None (no crash)."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    cwd = str(tmp_path)
    session_id = "s2"

    resume_calls = []

    def tracking_factory(sdk_cwd, cfg_dir, can_use_tool, mode, resume):
        resume_calls.append(resume)
        from server.tests.fakes.fake_sdk import FakeSDKClient
        return FakeSDKClient([], on_can_use_tool=can_use_tool)

    async def _run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(
                id=session_id, name="test", account_name="acct1",
                claude_session_id=None, status="idle", cwd=cwd,
            ))
        orch = SessionOrchestrator(sdk_factory=tracking_factory)
        await orch.rehydrate()

        cfg_dir = tmp_path / "cfg" / "acct1"
        cfg_dir.mkdir(parents=True)

        with patch("server.sessions.orchestrator.compose", return_value=cfg_dir), \
             patch("server.sessions.orchestrator.configdir"):
            driver = await orch.ensure_driver(session_id)

        return driver, resume_calls

    driver, resume_calls = asyncio.run(_run())

    assert driver is not None
    # resume should be None (not crash, not empty-string)
    assert resume_calls == [None], f"expected resume=None, got {resume_calls}"


# -----------------------------------------------------------------------
# Test 3: ensure_driver is idempotent for live sessions
# -----------------------------------------------------------------------

def test_ensure_driver_returns_existing_driver(tmp_path):
    """If a live driver exists, ensure_driver returns it without calling the factory again."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    cwd = str(tmp_path)
    session_id = "s3"

    factory_calls = []

    def counting_factory(sdk_cwd, cfg_dir, can_use_tool, mode, resume):
        factory_calls.append(1)
        from server.tests.fakes.fake_sdk import FakeSDKClient
        return FakeSDKClient([], on_can_use_tool=can_use_tool)

    async def _run():
        await init_db(db_url=db_url)
        orch = SessionOrchestrator(sdk_factory=counting_factory)

        cfg_dir = tmp_path / "cfg" / "acct1"
        cfg_dir.mkdir(parents=True)

        with patch("server.sessions.orchestrator.compose", return_value=cfg_dir), \
             patch("server.sessions.orchestrator.select_account", return_value="acct1"), \
             patch("server.sessions.orchestrator.configdir"):
            # create_session builds a live driver via the factory
            record = await orch.create_session("live-session", account="acct1", cwd=cwd)
            live_sid = record.id
            factory_before = len(factory_calls)

            # ensure_driver should return the existing driver
            driver_first = await orch.ensure_driver(live_sid)
            driver_second = await orch.ensure_driver(live_sid)

        return driver_first, driver_second, factory_before, len(factory_calls)

    d1, d2, before, after = asyncio.run(_run())

    assert d1 is d2, "ensure_driver should return the same driver on repeated calls"
    assert after == before, "ensure_driver must not call the factory again for a live driver"


# -----------------------------------------------------------------------
# Test 4: ensure_driver returns None for unknown session
# -----------------------------------------------------------------------

def test_ensure_driver_unknown_session_returns_none(tmp_path):
    """ensure_driver returns None when the session_id doesn't exist in records or DB."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"

    async def _run():
        await init_db(db_url=db_url)
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        # No rehydrate — empty state
        return await orch.ensure_driver("nonexistent")

    result = asyncio.run(_run())
    assert result is None
