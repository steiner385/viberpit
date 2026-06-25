"""Thread 2 — proactive idle-token refresh, decoupled from the usage poller's
backoff. Keeps idle accounts' access tokens fresh (and recovers quickly after a
daemon restart) even when the usage endpoint is throttled.
"""
import asyncio
from unittest import mock

import pytest

from server.sessions.orchestrator import SessionOrchestrator


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
    refreshed = [c.args[0] for c in ca.ensure_valid_token.call_args_list]
    assert refreshed == ["idle_ok"], f"only the idle healthy account should refresh: {refreshed}"


@pytest.mark.asyncio
async def test_refresh_idle_tokens_is_best_effort():
    """A refresh raising (AuthError/TransientError/anything) must not abort the
    pass — other accounts still get their turn."""
    orch = _orch()
    orch.has_live_session = lambda name: False
    with mock.patch("server.sessions.orchestrator.ccswitch_accounts") as ca:
        ca.list_accounts.return_value = [{"name": "a", "status": "ok"}, {"name": "b", "status": "ok"}]
        ca.ensure_valid_token.side_effect = [RuntimeError("boom"), None]
        await orch._refresh_idle_tokens_once()  # must not raise
    assert [c.args[0] for c in ca.ensure_valid_token.call_args_list] == ["a", "b"]


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
