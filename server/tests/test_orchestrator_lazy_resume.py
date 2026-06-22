"""Tests for Task 4: ensure_driver lazily resumes a detached session on attach."""
from __future__ import annotations

import asyncio
import os
from unittest.mock import patch


from server.db import init_db, db_session
from server.db.models import Session as DbSession
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


def test_ensure_driver_falls_back_to_fresh_when_resume_fails(tmp_path):
    """If resuming the stored claude_session_id fails (e.g. the session lived under a
    different cwd/project, or was pruned), ensure_driver retries with resume=None so
    the session stays USABLE rather than erroring out."""
    from server.tests.fakes.fake_sdk import FakeSDKClient
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    cwd = str(tmp_path)
    session_id = "s-fallback"
    resume_calls = []

    def factory(sdk_cwd, cfg_dir, can_use_tool, mode, resume):
        resume_calls.append(resume)
        c = FakeSDKClient([], on_can_use_tool=can_use_tool)
        if resume is not None:
            # Simulate `claude --resume <id>` failing to find the session.
            async def _boom():
                raise RuntimeError("Command failed with exit code 1")
            c.__aenter__ = lambda: _boom()
        return c

    async def _run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(
                id=session_id, name="t", account_name="acct1",
                claude_session_id="stale-unresumable-id", status="idle", cwd=cwd,
            ))
        orch = SessionOrchestrator(sdk_factory=factory)
        await orch.rehydrate()
        cfg_dir = tmp_path / "cfg" / "acct1"
        cfg_dir.mkdir(parents=True)
        with patch("server.sessions.orchestrator.compose", return_value=cfg_dir), \
             patch("server.sessions.orchestrator.configdir"):
            driver = await orch.ensure_driver(session_id)
        return orch, driver, resume_calls

    orch, driver, resume_calls = asyncio.run(_run())
    assert driver is not None, "resume failure should fall back to a usable fresh driver"
    assert resume_calls == ["stale-unresumable-id", None], (
        f"expected a resume attempt then a fresh retry, got {resume_calls}"
    )
    assert orch.driver(session_id) is driver
    assert orch.get_session(session_id).detached is False


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


# -----------------------------------------------------------------------
# Test 4b: ensure_driver threads the attaching identity (regression)
# -----------------------------------------------------------------------

def test_ensure_driver_threads_attaching_identity(tmp_path):
    """A resumed driver must carry the attaching user's identity, not a hardcoded
    "system" sentinel.

    Regression: AskUserQuestion / permission answers register under
    ``driver.identity``; the WebSocket resolves them with the real Tailscale
    identity. When ensure_driver hardcoded identity="system", the identity check
    in the registries rejected every answer on a *resumed* session — the answer
    was silently dropped and the agent hung forever awaiting it.
    """
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    cwd = str(tmp_path)
    session_id = "ident-s1"
    user = "tony@kindash.com"

    async def _run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(
                id=session_id, name="test", account_name="acct1",
                claude_session_id="resume-me", status="idle", cwd=cwd,
            ))
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        await orch.rehydrate()

        cfg_dir = tmp_path / "cfg" / "acct1"
        cfg_dir.mkdir(parents=True)

        with patch("server.sessions.orchestrator.compose", return_value=cfg_dir), \
             patch("server.sessions.orchestrator.configdir"):
            driver = await orch.ensure_driver(session_id, identity=user)

            # Register a pending question the way the SDK callback does — under
            # the driver's own identity.
            qid, fut = driver._qreg.request(
                session_id, "t1", "H", "Q?", [{"label": "Yes"}], False, driver.identity,
            )
            # The user answers over the WS with their real identity.
            resolved = orch.answer_question(session_id, qid, ["Yes"], user)
            answer = await fut if fut.done() else None
        return driver.identity, resolved, answer

    identity, resolved, answer = asyncio.run(_run())

    assert identity == user, (
        f"resumed driver must carry the attaching identity, got {identity!r}"
    )
    assert resolved is True, "answer_question must resolve with the attaching identity"
    assert answer == (["Yes"], "option"), "the pending question future must receive the (answer, mode) tuple"


# -----------------------------------------------------------------------
# Test 5: ensure_driver is safe under concurrent calls (TOCTOU fix)
# -----------------------------------------------------------------------

def test_ensure_driver_concurrent_calls_start_driver_once(tmp_path):
    """Two concurrent ensure_driver calls for the same detached session must:
    - return the SAME driver object (identity check)
    - start only ONE SDK process (factory called exactly once)

    The factory used here wraps FakeSDKClient with a yielding __aenter__ so
    that the asyncio event loop actually suspends between the _drivers.get()
    check and the _drivers[session_id] = driver registration.  That artificial
    yield makes the TOCTOU race observable: without the asyncio.Lock both
    coroutines pass the "no driver yet" gate before either registers its
    driver, then both build+start one.  With the lock the re-check inside the
    critical section makes the second coroutine reuse the first driver.
    """
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    cwd = str(tmp_path)
    session_id = "concurrent-s1"
    claude_resume_id = "claude-concurrent-resume"

    factory_call_count = 0

    class _YieldingFakeClient:
        """FakeSDKClient wrapper whose __aenter__ yields to the event loop,
        making the TOCTOU window in ensure_driver observable to asyncio.gather."""

        def __init__(self, inner):
            self._inner = inner

        async def __aenter__(self):
            # Yield control so a concurrent ensure_driver can slip through the
            # _drivers.get() check before we reach _drivers[session_id] = driver.
            await asyncio.sleep(0)
            return await self._inner.__aenter__()

        async def __aexit__(self, *exc):
            return await self._inner.__aexit__(*exc)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def yielding_factory(sdk_cwd, cfg_dir, can_use_tool, mode, resume):
        nonlocal factory_call_count
        factory_call_count += 1
        from server.tests.fakes.fake_sdk import FakeSDKClient
        return _YieldingFakeClient(FakeSDKClient([], on_can_use_tool=can_use_tool))

    async def _run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(
                id=session_id,
                name="concurrent-test",
                account_name="acct1",
                claude_session_id=claude_resume_id,
                status="idle",
                cwd=cwd,
            ))
        orch = SessionOrchestrator(sdk_factory=yielding_factory)
        await orch.rehydrate()

        cfg_dir = tmp_path / "cfg" / "acct1"
        cfg_dir.mkdir(parents=True)

        with patch("server.sessions.orchestrator.compose", return_value=cfg_dir), \
             patch("server.sessions.orchestrator.configdir"):
            driver_a, driver_b = await asyncio.gather(
                orch.ensure_driver(session_id),
                orch.ensure_driver(session_id),
            )

        return driver_a, driver_b

    driver_a, driver_b = asyncio.run(_run())

    assert driver_a is not None, "first concurrent call returned None"
    assert driver_b is not None, "second concurrent call returned None"
    assert driver_a is driver_b, (
        "concurrent ensure_driver calls must return the SAME driver object; "
        "got two distinct drivers — TOCTOU race detected"
    )
    assert factory_call_count == 1, (
        f"sdk_factory must be called exactly once; called {factory_call_count} times — "
        "two SDK processes would attach to the same Claude conversation"
    )


def test_ensure_driver_recreates_missing_cwd(tmp_path):
    """A rehydrated session whose cwd (scratch dir / worktree) was removed on close
    must have it recreated, not fail with 'Working directory does not exist'."""
    from server.tests.fakes.fake_sdk import FakeSDKClient
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    missing_cwd = str(tmp_path / "gone" / "session-cwd")  # does NOT exist
    session_id = "s-missing-cwd"

    def factory(cwd, cfg, can_use_tool, mode, resume):
        return FakeSDKClient([], on_can_use_tool=can_use_tool)

    async def _run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(
                id=session_id, name="t", account_name="acct1",
                claude_session_id="csid", status="idle", cwd=missing_cwd,
            ))
        orch = SessionOrchestrator(sdk_factory=factory)
        await orch.rehydrate()
        cfg_dir = tmp_path / "cfg"
        cfg_dir.mkdir(parents=True)
        with patch("server.sessions.orchestrator.compose", return_value=cfg_dir), \
             patch("server.sessions.orchestrator.configdir"):
            driver = await orch.ensure_driver(session_id)
        return driver

    driver = asyncio.run(_run())
    assert driver is not None
    assert os.path.isdir(missing_cwd), "ensure_driver must recreate the missing cwd"
