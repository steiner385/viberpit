"""Disabled-account invariants.

Three contracts introduced alongside the enable/disable toggle:
1. UsagePoller.all() excludes disabled accounts from the poll sweep.
2. _refresh_idle_tokens_once() skips disabled accounts.
3. _check_disabled_account_sessions() rotates live sessions off disabled accounts.
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord
from server.usage.poller import UsagePoller


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _rec(o, account, *, status="idle"):
    rec = SessionRecord(id="s1", name="n", account=account, engine=AgentEngine.CLAUDE_CODE, engine_session_id="c1",
                        cols=80, rows=24, cwd="/tmp", status=status)
    o._records["s1"] = rec
    o._drivers["s1"] = object()
    return rec


def _make_orch():
    o = SessionOrchestrator(sdk_factory=lambda *a, **k: None)
    o._clock = lambda: 1000.0
    o.rotated = []

    async def spy_rotate(record, reason):
        o.rotated.append((record.id, record.account, reason))

    o._rotate_away = spy_rotate
    return o


# ---------------------------------------------------------------------------
# 1. UsagePoller.all() skips disabled accounts
# ---------------------------------------------------------------------------

def test_poller_all_excludes_disabled():
    """UsagePoller.all() must not fetch usage for disabled accounts."""
    fetched = []

    async def run():
        p = UsagePoller.__new__(UsagePoller)
        p._fail_count = {}
        p._cache = {}
        p._fetch_locks = {}
        p._subscribers = []
        p._executor = None

        async def fake_get(name):
            fetched.append(name)
            return None

        p.get = fake_get

        accounts = [
            {"name": "enabled-acct", "enabled": True},
            {"name": "disabled-acct", "enabled": False},
            {"name": "implicit-enabled"},  # no "enabled" key → defaults to enabled
        ]
        with patch("server.usage.poller.list_accounts", return_value=accounts):
            await p.all()

    asyncio.run(run())

    assert "enabled-acct" in fetched
    assert "implicit-enabled" in fetched
    assert "disabled-acct" not in fetched


# ---------------------------------------------------------------------------
# 2. _refresh_idle_tokens_once() skips disabled accounts
# ---------------------------------------------------------------------------

def test_token_refresh_skips_disabled():
    """_refresh_idle_tokens_once must skip accounts with enabled=False."""
    refreshed = []

    async def run():
        o = SessionOrchestrator(sdk_factory=lambda *a, **k: None)
        accounts = [
            {"name": "alice", "status": "ok", "enabled": True},
            {"name": "bob",   "status": "ok", "enabled": False},
        ]
        import ccswitch.accounts as ca
        with patch.object(ca, "list_accounts", return_value=accounts), \
             patch.object(ca, "ensure_valid_token", side_effect=refreshed.append):
            await o._refresh_idle_tokens_once()

    asyncio.run(run())

    assert "alice" in refreshed
    assert "bob" not in refreshed


# ---------------------------------------------------------------------------
# 3. _check_disabled_account_sessions() rotates live sessions off disabled
# ---------------------------------------------------------------------------

def test_check_disabled_rotates_live_sessions():
    """Sessions on a disabled account should be rotated when the sweep runs."""
    async def run():
        o = _make_orch()
        rec = _rec(o, "disabled-acct", status="idle")

        from ccswitch import ledger
        import ccswitch.accounts as ca

        def fake_all_accounts():
            return [("disabled-acct", "anthropic"), ("other-acct", "anthropic")]

        with patch.object(ca, "all_accounts", side_effect=fake_all_accounts), \
             patch.object(ledger, "locked_ledger") as mock_ll:
            # Make locked_ledger return a context manager that yields a fake ledger
            # where "disabled-acct" is disabled
            fake_led = {"accounts": {"disabled-acct": {"enabled": False},
                                     "other-acct": {"enabled": True}}}
            mock_ll.return_value.__enter__ = lambda s: fake_led
            mock_ll.return_value.__exit__ = MagicMock(return_value=False)

            # _spawn is synchronous fire-and-forget; replace with direct await
            spawned = []
            def fake_spawn(coro, name=None):
                spawned.append(asyncio.ensure_future(coro))

            o._spawn = fake_spawn
            await o._check_disabled_account_sessions()
            # drain any spawned coroutines
            if spawned:
                await asyncio.gather(*spawned)

        assert o.rotated, "expected a rotation to be triggered"
        assert o.rotated[0] == ("s1", "disabled-acct", "account_disabled")

    asyncio.run(run())


def test_check_disabled_skips_inactive():
    """Inactive (archived/ended) sessions on a disabled account are skipped."""
    async def run():
        o = _make_orch()
        rec = _rec(o, "disabled-acct", status="archived")

        from ccswitch import ledger
        import ccswitch.accounts as ca

        with patch.object(ca, "all_accounts", return_value=[("disabled-acct", "anthropic")]), \
             patch.object(ledger, "locked_ledger") as mock_ll:
            fake_led = {"accounts": {"disabled-acct": {"enabled": False}}}
            mock_ll.return_value.__enter__ = lambda s: fake_led
            mock_ll.return_value.__exit__ = MagicMock(return_value=False)

            o._spawn = lambda coro, name=None: None
            await o._check_disabled_account_sessions()

        assert not o.rotated, "archived sessions must not be rotated"

    asyncio.run(run())


def test_check_disabled_noop_when_all_enabled():
    """When all accounts are enabled, the sweep touches nothing."""
    async def run():
        o = _make_orch()
        _rec(o, "good-acct", status="idle")

        from ccswitch import ledger
        import ccswitch.accounts as ca

        with patch.object(ca, "all_accounts", return_value=[("good-acct", "anthropic")]), \
             patch.object(ledger, "locked_ledger") as mock_ll:
            fake_led = {"accounts": {"good-acct": {"enabled": True}}}
            mock_ll.return_value.__enter__ = lambda s: fake_led
            mock_ll.return_value.__exit__ = MagicMock(return_value=False)

            o._spawn = lambda coro, name=None: None
            await o._check_disabled_account_sessions()

        assert not o.rotated

    asyncio.run(run())
