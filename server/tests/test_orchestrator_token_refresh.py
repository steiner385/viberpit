"""Thread 2 — proactive idle-token refresh, decoupled from the usage poller's
backoff. Keeps idle accounts' access tokens fresh (and recovers quickly after a
daemon restart) even when the usage endpoint is throttled.
"""
import asyncio
from unittest import mock

import pytest

from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord


def _orch():
    return SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda n: None)


@pytest.mark.asyncio
async def test_refresh_idle_tokens_skips_needs_reauth_and_live_sessions():
    orch = _orch()
    accts = [
        {"name": "idle_ok", "status": "ok"},
        {"name": "dead", "status": "needs-reauth"},   # dead token — don't hammer
        {"name": "live_ok", "status": "ok"},           # SDK owns the token family
    ]
    orch.has_live_session = lambda name: name == "live_ok"
    with mock.patch("server.sessions.orchestrator.ccswitch_accounts") as ca:
        ca.list_accounts.return_value = accts
        await orch._refresh_idle_tokens_once()
    refreshed = [c.args[0] for c in ca.acquire_valid_token.call_args_list]
    assert refreshed == ["idle_ok"], f"only the idle healthy account should refresh: {refreshed}"


@pytest.mark.asyncio
async def test_refresh_idle_tokens_is_best_effort():
    """A refresh raising (AuthError/TransientError/anything) must not abort the
    pass — other accounts still get their turn."""
    orch = _orch()
    orch.has_live_session = lambda name: False
    with mock.patch("server.sessions.orchestrator.ccswitch_accounts") as ca:
        ca.list_accounts.return_value = [{"name": "a", "status": "ok"}, {"name": "b", "status": "ok"}]
        ca.acquire_valid_token.side_effect = [RuntimeError("boom"), None]
        await orch._refresh_idle_tokens_once()  # must not raise
    assert [c.args[0] for c in ca.acquire_valid_token.call_args_list] == ["a", "b"]


@pytest.mark.asyncio
async def test_mark_needs_reauth_writes_audit_event():
    from sqlalchemy import select
    from server.db import init_db, db_session
    from server.db.models import AuditEvent
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    orch = _orch()
    await orch._mark_needs_reauth_audit("primary")
    async with db_session() as db:
        rows = (await db.execute(select(AuditEvent).where(
            AuditEvent.type == "account.needs_reauth"))).scalars().all()
    assert len(rows) == 1 and rows[0].detail["path"] == "live-session" and rows[0].detail["account"] == "primary"


@pytest.mark.asyncio
async def test_mark_needs_reauth_audit_records_token_expiry(monkeypatch):
    from sqlalchemy import select
    from server.db import init_db, db_session
    from server.db.models import AuditEvent
    from server.sessions import orchestrator as orch_mod
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    orch = _orch()
    ca = mock.MagicMock()
    ca._slot_expires_at.return_value = 1893456000000  # fixed ms-epoch
    monkeypatch.setattr(orch_mod, "ccswitch_accounts", ca)
    await orch._mark_needs_reauth_audit("primary")
    async with db_session() as db:
        rows = (await db.execute(select(AuditEvent).where(
            AuditEvent.type == "account.needs_reauth"))).scalars().all()
    assert len(rows) == 1
    assert rows[0].detail["token_expires_at"] == 1893456000000
    ca._slot_expires_at.assert_called_once_with("primary")


@pytest.mark.asyncio
async def test_start_launches_token_refresh_monitor_and_stop_cancels():
    orch = _orch()
    orch._refresh_idle_tokens_once = mock.AsyncMock()
    # Avoid the real poller/PR-poller and sibling monitors doing work in the test.
    orch._poller = mock.AsyncMock()
    orch._pr_poller = mock.AsyncMock()
    orch._rotation_monitor = mock.AsyncMock()
    orch._titling_monitor = mock.AsyncMock()
    await orch.start()
    assert orch._token_refresh_task is not None
    # The monitor runs an immediate pass on startup (restart recovery).
    for _ in range(10):
        await asyncio.sleep(0)
        if orch._refresh_idle_tokens_once.await_count:
            break
    assert orch._refresh_idle_tokens_once.await_count >= 1
    task = orch._token_refresh_task
    await orch.stop()
    assert task.cancelled() or task.done()
    assert orch._token_refresh_task is None  # stop() nulls the handle


def test_poller_usage_fn_tags_path_poller_idle(monkeypatch):
    """The default _poller_usage_fn must pass path="poller-idle" to usage_for."""
    import server.sessions.orchestrator as orch_mod
    seen: dict = {}

    def fake_usage_for(name, *, allow_refresh=True, path="unknown"):
        seen["path"] = path
        seen["name"] = name
        return None

    monkeypatch.setattr(orch_mod.ccswitch_accounts, "usage_for", fake_usage_for)
    # Build WITHOUT a usage_fn so the real _poller_usage_fn wrapper is created.
    orch = orch_mod.SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0)
    orch._poller._usage_fn("primary")  # call the wired poller fn directly
    assert seen == {"path": "poller-idle", "name": "primary"}


def test_session_factory_injects_oauth_token_env(monkeypatch):
    """Session factory must inject CLAUDE_CODE_OAUTH_TOKEN for accounts with a setup-token."""
    captured = {}
    def base(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, on_pre_compact=None):
        captured["extra_env"] = extra_env
        return object()
    orch = SessionOrchestrator(sdk_factory=base, poll_interval=9999.0, usage_fn=lambda n: None)
    orch._x11 = None
    orch._records["s1"] = SessionRecord(id="s1", name="n", account="primary",
                                        engine=AgentEngine.CLAUDE_CODE,
                                        engine_session_id="", cols=80, rows=24, cwd="/tmp/x")
    from server.sessions import account_auth
    monkeypatch.setattr(account_auth, "crypto", mock.MagicMock())
    account_auth.crypto.read_oauth_token = lambda acct: "sk-ant-oat01-TOK" if acct == "primary" else None
    factory = orch._session_factory("s1")
    factory("/tmp/x", "/cfg", None, "default", None)
    assert captured["extra_env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-TOK"


def test_session_factory_no_token_leaves_env_unchanged(monkeypatch):
    """When no setup-token exists for the account, factory must NOT inject
    CLAUDE_CODE_OAUTH_TOKEN and (with no X11) must call the base factory WITHOUT extra_env."""
    captured = {}
    def base(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, on_pre_compact=None):
        captured["extra_env"] = extra_env
        return object()
    orch = SessionOrchestrator(sdk_factory=base, poll_interval=9999.0, usage_fn=lambda n: None)
    orch._x11 = None
    orch._records["s1"] = SessionRecord(id="s1", name="n", account="primary",
                                        engine=AgentEngine.CLAUDE_CODE,
                                        engine_session_id="", cols=80, rows=24, cwd="/tmp/x")
    from server.sessions import account_auth
    monkeypatch.setattr(account_auth, "crypto", mock.MagicMock())
    account_auth.crypto.read_oauth_token = lambda acct: None
    factory = orch._session_factory("s1")
    factory("/tmp/x", "/cfg", None, "default", None)
    # no token + no x11 → base called with extra_env=None (unchanged old path)
    assert captured["extra_env"] is None
