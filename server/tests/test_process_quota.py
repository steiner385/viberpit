"""ProcessQuotaRefresher: cold reads for Copilot and Codex quota.

The policy under test is the one that makes a Copilot or Codex account show
usage with no session open, the way a Claude account does: start the process
when the ledger is cold, read once, hand it back to the idle reaper, and serve
the ledger in between. Sources are faked; the two real adapters get one
integration test each against their real manager surface.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from ccswitch import account_registry, accounts, ledger, paths
from ccswitch.config import load_config
from ccswitch.quota import UNKNOWN, QuotaSnapshot, copilot_snapshot, unknown_snapshot
from server.usage.process_quota import (
    CodexQuotaSource,
    CopilotQuotaSource,
    ProcessQuotaRefresher,
)
from tests.helpers import temp_env

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


class FakeSource:
    """A scripted QuotaSource: what is running, what a read yields, what fails."""

    def __init__(self, provider: str, *, snapshot: QuotaSnapshot | None = None,
                 start_error: Exception | None = None, is_available: bool = True) -> None:
        self.provider = provider
        self.snapshot = snapshot
        self.start_error = start_error
        self.is_available = is_available
        self.running_accounts: set[str] = set()
        self.starts: list[str] = []
        self.reads: list[str] = []
        self.releases: list[str] = []

    def available(self) -> bool:
        return self.is_available

    def running(self, account: str) -> bool:
        return account in self.running_accounts

    async def start(self, account: str) -> None:
        self.starts.append(account)
        if self.start_error is not None:
            raise self.start_error
        self.running_accounts.add(account)

    async def read(self, account: str) -> QuotaSnapshot | None:
        self.reads.append(account)
        return self.snapshot

    def release(self, account: str) -> None:
        self.releases.append(account)


def _seed_copilot(name: str) -> None:
    d = paths.copilot_token_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "credentials.json").write_text("{}")


def _record(name: str, snapshot: QuotaSnapshot, age: timedelta) -> None:
    with ledger.locked_ledger() as led:
        ledger.record_usage(led, name, snapshot, datetime.now(timezone.utc) - age)


def _cooldown(name: str):
    with ledger.locked_ledger() as led:
        acct = led["accounts"].get(name, {})
        return acct.get("usageFetchFails"), acct.get("usageFetchCooldownUntil")


def _refresher(*sources, **kw) -> ProcessQuotaRefresher:
    return ProcessQuotaRefresher(sources, sweep_interval=9999.0, **kw)


def _snap(pct: float = 30.0) -> QuotaSnapshot:
    return copilot_snapshot(pct, None, fetched_at=datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Cold path
# ---------------------------------------------------------------------------

def test_cold_ledger_starts_reads_persists_and_releases():
    """The whole point: no session, no process, no reading → one short-lived
    process, a persisted snapshot, and the process handed to the idle reaper."""
    with temp_env():
        _seed_copilot("cp")
        src = FakeSource("copilot", snapshot=_snap(30.0))
        got = asyncio.run(_refresher(src).refresh("cp"))
        assert got is not None and got.window("premium").used_pct == 30.0
        assert src.starts == ["cp"] and src.reads == ["cp"] and src.releases == ["cp"]
        # The synchronous core now serves it without touching a process.
        u = accounts.usage_for("cp")
        assert u is not None and u.window("premium").used_pct == 30.0


def test_fresh_ledger_is_served_without_a_start():
    with temp_env():
        _seed_copilot("cp")
        _record("cp", _snap(55.0), timedelta(minutes=2))
        src = FakeSource("copilot", snapshot=_snap(1.0))
        got = asyncio.run(_refresher(src).refresh("cp"))
        assert got.window("premium").used_pct == 55.0
        assert src.starts == [] and src.reads == []


def test_ledger_past_the_process_ttl_triggers_a_cold_read():
    with temp_env():
        cfg = load_config()
        _seed_copilot("cp")
        _record("cp", _snap(55.0), timedelta(seconds=cfg.process_quota_ttl_sec + 1))
        src = FakeSource("copilot", snapshot=_snap(60.0))
        got = asyncio.run(_refresher(src).refresh("cp"))
        assert got.window("premium").used_pct == 60.0
        assert src.starts == ["cp"] and src.releases == ["cp"]


def test_force_bypasses_a_fresh_ledger():
    with temp_env():
        _seed_copilot("cp")
        _record("cp", _snap(55.0), timedelta(seconds=5))
        src = FakeSource("copilot", snapshot=_snap(60.0))
        got = asyncio.run(_refresher(src).refresh("cp", force=True))
        assert got.window("premium").used_pct == 60.0
        assert src.starts == ["cp"]


# ---------------------------------------------------------------------------
# Live path: a running process is read on the REST cadence, never released
# ---------------------------------------------------------------------------

def test_running_process_is_read_without_start_or_release():
    with temp_env():
        cfg = load_config()
        _seed_copilot("cp")
        # Older than the REST TTL, younger than the process TTL: a cold process
        # would be left alone, a live one is cheap to re-ask.
        _record("cp", _snap(10.0), timedelta(seconds=cfg.usage_cache_ttl_sec + 1))
        src = FakeSource("copilot", snapshot=_snap(12.0))
        src.running_accounts.add("cp")
        got = asyncio.run(_refresher(src).refresh("cp"))
        assert got.window("premium").used_pct == 12.0
        assert src.reads == ["cp"] and src.starts == [] and src.releases == []


def test_running_process_is_throttled_by_the_rest_ttl():
    with temp_env():
        _seed_copilot("cp")
        _record("cp", _snap(10.0), timedelta(seconds=5))
        src = FakeSource("copilot", snapshot=_snap(12.0))
        src.running_accounts.add("cp")
        got = asyncio.run(_refresher(src).refresh("cp"))
        assert got.window("premium").used_pct == 10.0
        assert src.reads == []


# ---------------------------------------------------------------------------
# Failure handling shares the ledger's usage-fetch cooldown
# ---------------------------------------------------------------------------

def test_start_failure_arms_the_shared_cooldown_and_is_not_retried_until_it_lapses():
    with temp_env():
        _seed_copilot("cp")
        src = FakeSource("copilot", start_error=RuntimeError("copilot2api binary not found"))
        r = _refresher(src)
        assert asyncio.run(r.refresh("cp")) is None
        fails, until = _cooldown("cp")
        assert fails == 1 and until is not None
        assert src.releases == []          # nothing started, nothing to release
        assert asyncio.run(r.refresh("cp")) is None
        assert src.starts == ["cp"]        # cooldown honoured: no second spawn
        # The manual refresh button bypasses the cooldown.
        assert asyncio.run(r.refresh("cp", force=True)) is None
        assert src.starts == ["cp", "cp"]


def test_unreadable_process_is_still_released_and_nothing_is_persisted():
    with temp_env():
        _seed_copilot("cp")
        src = FakeSource("copilot", snapshot=None)
        assert asyncio.run(_refresher(src).refresh("cp")) is None
        assert src.releases == ["cp"]
        assert _cooldown("cp")[0] == 1
        assert accounts.usage_for("cp") is None


def test_degraded_snapshot_is_returned_but_never_persisted():
    """A current outage must be reported as such, not masked by an older
    reading — the API's Codex path relies on the returned availability."""
    with temp_env():
        account_registry.register_account("chatgpt", account_registry.PROVIDER_OPENAI,
                                          engine="codex")
        degraded = unknown_snapshot("openai", availability=UNKNOWN)
        src = FakeSource("openai", snapshot=degraded)
        got = asyncio.run(_refresher(src).refresh("chatgpt"))
        assert got is degraded
        with ledger.locked_ledger() as led:
            assert "usage" not in led["accounts"].get("chatgpt", {})


def test_persist_false_returns_the_reading_without_writing_it():
    """Fleet workers answer the edge with persist=False; the edge persists the
    merged result once (fleet_orchestrator.codex_account_status)."""
    with temp_env():
        _seed_copilot("cp")
        src = FakeSource("copilot", snapshot=_snap(30.0))
        got = asyncio.run(_refresher(src).refresh("cp", persist=False))
        assert got is not None
        assert accounts.usage_for("cp") is None


# ---------------------------------------------------------------------------
# Scope: which accounts, which hosts
# ---------------------------------------------------------------------------

def test_rest_backed_and_unknown_accounts_are_ignored():
    with temp_env():
        src = FakeSource("copilot", snapshot=_snap())
        assert asyncio.run(_refresher(src).refresh("nobody")) is None
        assert src.starts == []


def test_unavailable_provider_on_this_host_is_a_noop():
    """The remote-mode edge has no process managers; a worker without the
    codex binary has none for Codex. Neither may fail or spawn anything."""
    with temp_env():
        _seed_copilot("cp")
        src = FakeSource("copilot", snapshot=_snap(), is_available=False)
        assert asyncio.run(_refresher(src).refresh("cp")) is None
        assert src.starts == []


def test_sweep_covers_only_enabled_process_backed_accounts():
    with temp_env():
        _seed_copilot("cp-on")
        _seed_copilot("cp-off")
        accounts.set_account_enabled("cp-off", False)  # the ledger flag is the operative one
        paths.store_dir().mkdir(parents=True, exist_ok=True)
        paths.slot_path("claude").write_text('{"claudeAiOauth": {}}')
        src = FakeSource("copilot", snapshot=_snap())
        asyncio.run(_refresher(src).sweep())
        assert src.starts == ["cp-on"]


def test_concurrent_refreshes_share_one_cold_start():
    with temp_env():
        _seed_copilot("cp")
        src = FakeSource("copilot", snapshot=_snap())
        r = _refresher(src)

        async def go():
            await asyncio.gather(r.refresh("cp"), r.refresh("cp"), r.refresh("cp"))

        asyncio.run(go())
        assert src.starts == ["cp"]


# ---------------------------------------------------------------------------
# The real adapters against their real manager surfaces
# ---------------------------------------------------------------------------

def test_copilot_source_drives_the_real_proxy_manager(monkeypatch):
    from server.sessions import copilot_proxy as cp
    from server.tests.test_copilot_proxy_ensure import _mgr

    monkeypatch.setattr(cp, "resolve_copilot_proxy", lambda: "/usr/bin/copilot2api")

    async def go():
        mgr, procs = _mgr(idle_grace_s=0.05,
                          usage_fetch=lambda port: {"premium": {"used": 3, "limit": 10}})
        src = CopilotQuotaSource(lambda: mgr)
        assert src.available() and not src.running("c1")
        await src.start("c1")
        assert src.running("c1")
        snap = await src.read("c1")
        assert snap is not None and snap.window("premium").used_pct == 30.0
        src.release("c1")
        await asyncio.sleep(0.15)
        assert mgr.port_for("c1") is None   # handed to the idle reaper

    asyncio.run(go())


def test_copilot_source_is_unavailable_without_a_manager():
    src = CopilotQuotaSource(lambda: None)
    assert not src.available() and not src.running("c1")


def test_codex_source_reads_through_the_manager_client():
    class _Mgr:
        available = True

        def __init__(self) -> None:
            self.clients: dict[str, object] = {}
            self.ensured: list[str] = []

        def client_for(self, account):
            return self.clients.get(account)

        async def ensure(self, account):
            self.ensured.append(account)
            self.clients[account] = object()

    class _Reader:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        async def read(self, account, *, client):
            self.calls.append((account, client))
            return _snap(7.0)

    mgr, reader = _Mgr(), _Reader()
    src = CodexQuotaSource(lambda: mgr, lambda: reader)

    async def go():
        assert src.available() and not src.running("a")
        await src.start("a")
        assert mgr.ensured == ["a"] and src.running("a")
        snap = await src.read("a")
        assert snap.window("premium").used_pct == 7.0
        assert reader.calls == [("a", mgr.clients["a"])]
        src.release("a")   # ensure() armed the idle reaper; nothing to do

    asyncio.run(go())


def test_codex_source_is_unavailable_when_codex_is_not():
    class _Mgr:
        available = False

        def client_for(self, account):
            return None

    assert not CodexQuotaSource(lambda: _Mgr(), lambda: None).available()
    assert not CodexQuotaSource(lambda: None, lambda: None).available()


@pytest.mark.asyncio
async def test_start_and_stop_are_idempotent_and_reap_the_task():
    r = _refresher(FakeSource("copilot"), list_accounts=lambda: [])
    await r.start()
    task = r._task
    await r.start()
    assert r._task is task
    await r.stop()
    assert r._task is None
    assert task.done()
    await r.stop()
