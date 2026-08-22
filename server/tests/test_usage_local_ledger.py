"""Usage display reads the SHARED ledger locally before paying for an RPC.

Usage is cached poller output living in ledger.json. On the master host that file
is local, so reading it is a file read. Fetching it over the daemon RPC instead
costs 1-4s on this deployment, because the daemon's account store is an sshfs
mount — measured 2026-08-22: usage(tony_kindash.com) took 3.68s against a 3.0s
ceiling, so every account degraded to "unavailable" in the UI.

Two nested timeouts made that worse: USAGE_FETCH_TIMEOUT (outer, asyncio) and
USAGE_RPC_TIMEOUT (inner, httpx). Raising only the outer let the inner fire first
and raise DaemonUnavailable instead of a clean timeout.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone


from server.api import accounts as acct_api


class _Orch:
    """Returns a Usage, matching RemoteOrchestrator.usage()'s real contract."""

    def __init__(self):
        self.calls: list[str] = []

    async def usage(self, name):
        self.calls.append(name)
        from ccswitch.oauth import Usage
        return Usage(99.0, None, 99.0, None, False)


def test_fresh_local_usage_skips_the_rpc(monkeypatch):
    """A fresh ledger entry must be served locally — no RPC at all."""
    from ccswitch.oauth import Usage

    monkeypatch.setattr(acct_api, "_local_cached_usage",
                        lambda name: Usage(7.0, None, 12.0, None, False) if name == "a" else None)
    orch = _Orch()
    got = asyncio.run(acct_api._safe_usage(orch, "a", 3.0))
    assert got.session_pct == 7.0, "must return a Usage, not a dict"
    assert orch.calls == [], "a fresh local read must not hit the daemon"


def test_stale_local_usage_falls_back_to_the_rpc(monkeypatch):
    """Stale/missing cache must still resolve — correctness over latency."""
    monkeypatch.setattr(acct_api, "_local_cached_usage", lambda name: None)
    orch = _Orch()
    got = asyncio.run(acct_api._safe_usage(orch, "b", 3.0))
    assert got.session_pct == 99.0
    assert orch.calls == ["b"], "must fall back to the daemon when the cache is cold"


def test_local_read_failure_falls_back_not_raises(monkeypatch):
    """An unreadable/corrupt ledger must degrade to the RPC, never 500 the page."""
    def boom(name):
        raise OSError("ledger unreadable")

    monkeypatch.setattr(acct_api, "_local_cached_usage", boom)
    orch = _Orch()
    got = asyncio.run(acct_api._safe_usage(orch, "c", 3.0))
    assert got.session_pct == 99.0
    assert orch.calls == ["c"]


def test_rpc_timeout_still_degrades_to_none(monkeypatch):
    """The existing contract is unchanged when both paths fail."""
    monkeypatch.setattr(acct_api, "_local_cached_usage", lambda name: None)

    class Slow:
        async def usage(self, name):
            await asyncio.sleep(5)

    got = asyncio.run(acct_api._safe_usage(Slow(), "d", 0.05))
    assert got is None


def test_real_local_read_returns_a_usage_object(tmp_path, monkeypatch):
    """Contract test with a REAL ledger — no mocking of the function under test.

    The first version of this fix returned _usage_to_dict(...) from
    _local_cached_usage. Every mocked test passed and production 500'd with
    "'dict' object has no attribute 'session_pct'", because the mocks asserted
    the wrong contract. Exercise the real path so the type is actually checked.
    """

    from ccswitch import ledger

    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))
    now = datetime.now(timezone.utc).isoformat()
    with ledger.locked_ledger() as led:
        led["accounts"]["acct"] = {"usage": {
            "session_pct": 42.0, "session_resets_at": None,
            "weekly_pct": 13.0, "weekly_resets_at": None,
            "extra_usage_enabled": False, "fetched_at": now,
        }}

    got = acct_api._local_cached_usage("acct")
    assert got is not None
    assert got.session_pct == 42.0, "must be a Usage dataclass, not a dict"
    assert acct_api._usage_to_dict(got)["session"]["pct"] == 42.0
