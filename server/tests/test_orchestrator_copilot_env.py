"""Copilot proxy wiring in the orchestrator: env injection + ensure-before-spawn.

Tests:
  1. _session_factory closure passes ANTHROPIC_BASE_URL (not CLAUDE_CODE_OAUTH_TOKEN)
     when the session account is a copilot account and a CopilotProxyManager is wired.
  2. create_session awaits ensure(account) before building the driver for a copilot account.
  3. latest_suggestions / latest_summary return empty immediately for copilot sessions.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord
from server.tests.fakes.fake_factory import fake_sdk_factory


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

COPILOT_ENV = {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:9999",
    "ANTHROPIC_AUTH_TOKEN": "x",
    "ENABLE_TOOL_SEARCH": "true",
}


class FakeCopilotProxy:
    """Minimal CopilotProxyManager stub: records ensure() calls, returns sentinel env."""

    def __init__(self) -> None:
        self.ensure_calls: list[str] = []

    async def ensure(self, account: str) -> int:
        self.ensure_calls.append(account)
        return 9999

    def env_for(self, account: str) -> dict[str, str]:
        return dict(COPILOT_ENV)


def _make_copilot_home(tmp_path: Path, account: str) -> Path:
    """Create a COMPLETE copilot token-dir so provider_of(account) == 'copilot'.

    paths.copilot_token_dir(name) == CCSWITCH_HOME / 'store' / 'copilot' / name

    ``credentials.json`` matters: it is what ``copilot_auth`` writes when the
    device flow succeeds, and what ``copilot.copilot_account_names()`` has always
    required before calling a directory an account. A bare directory is a
    half-finished login, and the account manifest — like that enumeration —
    declines to register it. The predecessor ``provider_of`` disagreed: it probed
    only for the directory, so an abandoned device flow was classified as a
    Copilot account that enumeration would never list.
    """
    ccswitch_home = tmp_path / "ccswitch_home"
    ccswitch_home.mkdir()
    # token_dir lives under store/copilot/<name>
    token_dir = ccswitch_home / "store" / "copilot" / account
    token_dir.mkdir(parents=True)
    (token_dir / "credentials.json").write_text("{}")
    return ccswitch_home


# ---------------------------------------------------------------------------
# Test 1: _session_factory closure injects copilot env (unit-level, no DB)
# ---------------------------------------------------------------------------

def test_factory_closure_injects_copilot_env(tmp_path):
    """_session_factory should pass copilot proxy env when account is copilot."""
    account = "copilot-user"
    store_home = _make_copilot_home(tmp_path, account)

    seen: dict = {}

    def recording_factory(cwd, cfg, cut, mode, resume, extra_env=None, *, on_pre_compact=None):
        seen["extra_env"] = extra_env

        class _Fake:
            async def connect(self, *a, **k): ...
            async def disconnect(self): ...

        return _Fake()

    proxy = FakeCopilotProxy()
    with patch.dict(os.environ, {"CCSWITCH_HOME": str(store_home)}):
        orch = SessionOrchestrator(sdk_factory=recording_factory, copilot=proxy)
        # Seed a minimal SessionRecord so _oat_env can find the account
        sid = "test-sid-001"
        orch._records[sid] = SessionRecord(
            id=sid, name="Test", account=account, engine=AgentEngine.CLAUDE_CODE, engine_session_id="",
            cols=80, rows=24, cwd=str(tmp_path),
        )
        closure = orch._session_factory(sid)
        closure(str(tmp_path), str(tmp_path / "cfg"), None, "interactive", None)

    assert seen["extra_env"] is not None, "extra_env must not be None for copilot session"
    assert "ANTHROPIC_BASE_URL" in seen["extra_env"], "ANTHROPIC_BASE_URL must be in extra_env"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in seen["extra_env"], (
        "CLAUDE_CODE_OAUTH_TOKEN must NOT appear in copilot session env"
    )


# ---------------------------------------------------------------------------
# Test 2: create_session calls ensure(account) before building the driver
# ---------------------------------------------------------------------------

def _fake_usage(account: str):
    from datetime import datetime, timedelta, timezone
    from ccswitch.quota import anthropic_snapshot
    reset = datetime.now(timezone.utc) + timedelta(hours=5)
    return anthropic_snapshot(10.0, reset, 10.0, reset, False)


@pytest.mark.asyncio
async def test_create_session_calls_ensure(tmp_path):
    """create_session must await proxy.ensure(account) before building the driver."""
    account = "copilot-user"
    store_home = _make_copilot_home(tmp_path, account)
    shared_base_dir = tmp_path / "dot-claude"
    shared_base_dir.mkdir()
    (shared_base_dir / "settings.json").write_text("{}")
    (shared_base_dir / "projects").mkdir()
    cfg_dir = tmp_path / "cfg" / account
    cfg_dir.mkdir(parents=True)
    repo_root = tmp_path / "repos"
    repo_root.mkdir()
    db_path = tmp_path / "copilot_test.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"

    proxy = FakeCopilotProxy()

    seen: dict = {}
    _inner = fake_sdk_factory()

    def recording_factory(cwd, cfg, cut, mode, resume, extra_env=None, *, on_pre_compact=None):
        seen["extra_env"] = extra_env
        return _inner(cwd, cfg, cut, mode, resume, extra_env=extra_env, on_pre_compact=on_pre_compact)

    from ccswitch.selector import Picked
    from server.db import init_db

    env_patch = patch.dict(os.environ, {
        "CCSWITCH_HOME": str(store_home),
        "CLAUDE_CONFIG_DIR": str(shared_base_dir),
        "CCSWITCH_DB_URL": db_url,
    })
    compose_patch = patch("server.sessions.orchestrator.compose", return_value=cfg_dir)
    trust_patch = patch("server.sessions.orchestrator.configdir.trust_project")
    select_patch = patch(
        "server.sessions.orchestrator.select_account",
        return_value=Picked(account),
    )

    with env_patch, compose_patch, trust_patch, select_patch:
        await init_db(db_url=db_url)
        orch = SessionOrchestrator(
            sdk_factory=recording_factory,
            copilot=proxy,
            poll_interval=9999.0,
            usage_fn=_fake_usage,
            repo_root=str(repo_root),
        )
        await orch.start()
        try:
            rec = await orch.create_session("copilot-session", account=account)
        finally:
            await orch.stop()

    assert proxy.ensure_calls == [account], (
        f"ensure() must be called exactly once with '{account}', got {proxy.ensure_calls}"
    )
    assert seen.get("extra_env") is not None, "extra_env must be set for copilot session"
    assert "ANTHROPIC_BASE_URL" in seen["extra_env"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in seen["extra_env"]


# ---------------------------------------------------------------------------
# Test 3: latest_suggestions / latest_summary skip copilot accounts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_latest_suggestions_skipped_for_copilot(tmp_path):
    """latest_suggestions returns [] immediately for copilot-backed sessions."""
    account = "copilot-user"
    store_home = _make_copilot_home(tmp_path, account)

    proxy = FakeCopilotProxy()
    with patch.dict(os.environ, {"CCSWITCH_HOME": str(store_home)}):
        orch = SessionOrchestrator(sdk_factory=lambda *a, **k: None, copilot=proxy)
        sid = "test-sid-002"
        orch._records[sid] = SessionRecord(
            id=sid, name="Test", account=account, engine=AgentEngine.CLAUDE_CODE, engine_session_id="",
            cols=80, rows=24, cwd=str(tmp_path),
        )
        result = await orch.latest_suggestions(sid)
    assert result == [], f"Expected [] for copilot session, got {result!r}"


# ---------------------------------------------------------------------------
# Test 4: failed driver.start() must not leave a ghost record in _records
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_session_start_failure_leaves_no_record(tmp_path):
    """If driver.start() raises, the pre-seeded _records entry must be removed."""
    account = "copilot-user"
    store_home = _make_copilot_home(tmp_path, account)
    shared_base_dir = tmp_path / "dot-claude"
    shared_base_dir.mkdir()
    (shared_base_dir / "settings.json").write_text("{}")
    (shared_base_dir / "projects").mkdir()
    cfg_dir = tmp_path / "cfg" / account
    cfg_dir.mkdir(parents=True)
    repo_root = tmp_path / "repos"
    repo_root.mkdir()
    db_path = tmp_path / "start_failure_test.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"

    proxy = FakeCopilotProxy()

    # Build a factory whose driver's start() raises RuntimeError.
    from server.tests.fakes.fake_sdk import FakeSDKClient

    class _FailingSDKClient(FakeSDKClient):
        async def __aenter__(self):
            raise RuntimeError("simulated driver.start() failure")

    def failing_factory(cwd, cfg, cut, mode, resume, extra_env=None, *, on_pre_compact=None):
        return _FailingSDKClient([])

    from ccswitch.selector import Picked
    from server.db import init_db

    env_patch = patch.dict(os.environ, {
        "CCSWITCH_HOME": str(store_home),
        "CLAUDE_CONFIG_DIR": str(shared_base_dir),
        "CCSWITCH_DB_URL": db_url,
    })
    compose_patch = patch("server.sessions.orchestrator.compose", return_value=cfg_dir)
    trust_patch = patch("server.sessions.orchestrator.configdir.trust_project")
    select_patch = patch(
        "server.sessions.orchestrator.select_account",
        return_value=Picked(account),
    )

    with env_patch, compose_patch, trust_patch, select_patch:
        await init_db(db_url=db_url)
        orch = SessionOrchestrator(
            sdk_factory=failing_factory,
            copilot=proxy,
            poll_interval=9999.0,
            usage_fn=_fake_usage,
            repo_root=str(repo_root),
        )
        await orch.start()
        try:
            with pytest.raises((RuntimeError, Exception)):
                await orch.create_session("fail-session", account=account)
        finally:
            await orch.stop()

    assert orch._records == {}, (
        f"_records must be empty after a failed create_session, got {list(orch._records)}"
    )


@pytest.mark.asyncio
async def test_latest_summary_skipped_for_copilot(tmp_path):
    """latest_summary returns '' immediately for copilot-backed sessions."""
    account = "copilot-user"
    store_home = _make_copilot_home(tmp_path, account)

    proxy = FakeCopilotProxy()
    with patch.dict(os.environ, {"CCSWITCH_HOME": str(store_home)}):
        orch = SessionOrchestrator(sdk_factory=lambda *a, **k: None, copilot=proxy)
        sid = "test-sid-003"
        orch._records[sid] = SessionRecord(
            id=sid, name="Test", account=account, engine=AgentEngine.CLAUDE_CODE, engine_session_id="",
            cols=80, rows=24, cwd=str(tmp_path),
        )
        result = await orch.latest_summary(sid, viewing=True)
    assert result == "", f"Expected '' for copilot session, got {result!r}"
